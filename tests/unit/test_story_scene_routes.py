"""HTTP contract for the 起幕 routes (SC1-A, end route SC1-D).

SC2 builds its composer against exactly these shapes and status codes, so
this file is the contract's executable copy: the open response body, the
``null``-not-404 state answer, every structured refusal code, and the end
route's closed-session / nullable-narration answer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kokoro_link.api.dependencies import get_container
from kokoro_link.api.routes.story_scene import router
from kokoro_link.contracts.cloud_action_billing import (
    ACTION_STORY_SCENE_OPEN,
    ActionPriceChanged,
    client_quoted_price,
)
from kokoro_link.infrastructure.llm.cloud_refusal import (
    INSUFFICIENT_CREDITS_CODE,
    ExpectedCloudRefusal,
)
from kokoro_link.application.services.chat_turn_lease import (
    CONVERSATION_BUSY_CODE,
    ConversationBusyError,
)
from kokoro_link.application.services.story_scene_closing import SceneClosing
from kokoro_link.application.services.story_scene_quota import (
    StorySceneDailyLimitReached,
    StorySceneQuotaUnavailable,
)
from kokoro_link.application.services.story_scene_service import (
    SceneAlreadyOpen,
    SceneMaterialUnavailable,
    SceneNotOpen,
    SceneOpenFailed,
    SceneSessionMismatch,
    StorySceneOpening,
    StorySceneUnavailable,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.conversation import (
    Message,
    MessageKind,
    MessageRole,
)
from kokoro_link.domain.entities.story_scene_session import (
    SCENE_CLOSE_MANUAL,
    SCENE_LAYER_BEAT,
    StorySceneSession,
)
from kokoro_link.domain.value_objects.character_state import CharacterState


NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


def _character(id_: str = "c1") -> Character:
    return Character(
        id=id_,
        name="Mio",
        summary="",
        personality=(),
        interests=(),
        speaking_style="",
        boundaries=(),
        aspirations=(),
        appearance="",
        world_frame="modern",
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
    )


def _session(**overrides) -> StorySceneSession:  # noqa: ANN003
    base = dict(
        character_id="c1",
        conversation_id="conv-1",
        source_layer=SCENE_LAYER_BEAT,
        arc_id="arc-1",
        beat_id="beat-1",
        title="頂樓的獨奏",
        location="頂樓天台",
        mood="欲言又止",
        scene_type="encounter",
        dramatic_question="她會說出口嗎？",
        opened_at=NOW,
    )
    base.update(overrides)
    return StorySceneSession.open_scene(**base)


def _opening() -> StorySceneOpening:
    return StorySceneOpening(
        session=_session(),
        narration=Message(
            role=MessageRole.ASSISTANT,
            content="頂樓的風把譜架吹得直響。",
            kind=MessageKind.SCENE_NARRATION,
            created_at=NOW,
        ),
        character_message=Message(
            role=MessageRole.ASSISTANT,
            content="……你來了。",
            created_at=NOW,
        ),
    )


class _StubSceneService:
    def __init__(
        self,
        *,
        opening: StorySceneOpening | None = None,
        raises: Exception | None = None,
        current: StorySceneSession | None = None,
        closing: SceneClosing | None = None,
        end_raises: Exception | None = None,
    ) -> None:
        self._opening = opening
        self._raises = raises
        self._current = current
        self._closing = closing
        self._end_raises = end_raises
        self.opened_for: list[str] = []
        self.ended: list[tuple[str, str | None]] = []
        self.quotes: list[float | None] = []
        """What the *client's* quote looked like from inside the service —
        i.e. whether the route put the body's number into scope (SC3-C)."""

    async def open_scene(self, character, *, now=None):  # noqa: ANN001
        self.opened_for.append(character.id)
        self.quotes.append(client_quoted_price(ACTION_STORY_SCENE_OPEN))
        if self._raises is not None:
            raise self._raises
        return self._opening

    async def current_scene(self, character_id: str):  # noqa: ANN201
        return self._current

    async def end_scene(self, character, *, session_id=None, now=None):  # noqa: ANN001
        self.ended.append((character.id, session_id))
        if self._end_raises is not None:
            raise self._end_raises
        return self._closing


class _StubCharacterService:
    def __init__(self, character: Character | None) -> None:
        self._character = character

    async def get_character_entity(self, character_id: str) -> Character | None:
        if self._character is None or self._character.id != character_id:
            return None
        return self._character


@dataclass
class _StubContainer:
    story_scene_service: object | None
    character_service: _StubCharacterService = field(
        default_factory=lambda: _StubCharacterService(_character()),
    )


def _client(container: _StubContainer) -> TestClient:
    app = FastAPI()
    app.state.container = container
    app.dependency_overrides[get_container] = lambda: container
    app.include_router(router, prefix="/api/v1")
    return TestClient(app)


# ── open ─────────────────────────────────────────────────────────────


def test_open_returns_201_with_session_messages_and_chip_slot() -> None:
    service = _StubSceneService(opening=_opening())
    client = _client(_StubContainer(story_scene_service=service))

    res = client.post("/api/v1/characters/c1/story-scene")

    assert res.status_code == 201
    body = res.json()
    assert set(body) == {
        "session", "narration", "character_message", "suggested_actions",
    }
    session = body["session"]
    assert session["status"] == "open"
    assert session["source_layer"] == SCENE_LAYER_BEAT
    assert session["beat_id"] == "beat-1"
    assert session["title"] == "頂樓的獨奏"
    assert session["location"] == "頂樓天台"
    assert session["mood"] == "欲言又止"
    assert session["dramatic_question"] == "她會說出口嗎？"
    assert session["closed_at"] is None and session["closed_reason"] is None
    # the frontend distinguishes the two messages by kind, not by order
    assert body["narration"]["kind"] == "scene_narration"
    assert body["character_message"]["kind"] == "chat"
    assert body["character_message"]["role"] == "assistant"
    # SC1-C fills this; the shape is frozen now so it can
    assert body["suggested_actions"] == []


@pytest.mark.parametrize(
    ("error", "expected_status", "expected_code"),
    [
        (SceneAlreadyOpen("c1"), 409, "scene_in_progress"),
        (
            SceneMaterialUnavailable("nothing to play"),
            409,
            "no_material",
        ),
        (
            ConversationBusyError("conv-1"),
            409,
            CONVERSATION_BUSY_CODE,
        ),
        (
            StorySceneUnavailable("demo tier"),
            403,
            "story_scene_unavailable",
        ),
        (SceneOpenFailed("writer failed"), 502, "scene_open_failed"),
        (
            StorySceneDailyLimitReached(limit=3, used=3),
            429,
            "story_scene_daily_limit_reached",
        ),
        (
            StorySceneQuotaUnavailable("ledger unreadable"),
            503,
            "story_scene_quota_unavailable",
        ),
    ],
    ids=[
        "already-open", "no-material", "busy", "demo", "writer-failed",
        "daily-limit", "quota-unavailable",
    ],
)
def test_open_refusals_are_structured(
    error: Exception, expected_status: int, expected_code: str,
) -> None:
    service = _StubSceneService(raises=error)
    client = _client(_StubContainer(story_scene_service=service))

    res = client.post("/api/v1/characters/c1/story-scene")

    assert res.status_code == expected_status
    assert res.json()["detail"]["code"] == expected_code
    assert res.json()["detail"]["message"]


def test_open_on_a_foreign_character_is_404() -> None:
    client = _client(_StubContainer(
        story_scene_service=_StubSceneService(opening=_opening()),
        character_service=_StubCharacterService(None),
    ))

    assert client.post("/api/v1/characters/nope/story-scene").status_code == 404


def test_open_without_the_service_wired_is_503() -> None:
    client = _client(_StubContainer(story_scene_service=None))

    assert client.post("/api/v1/characters/c1/story-scene").status_code == 503


# ── open: the hosted quote (SC3-C) ───────────────────────────────────


def test_open_puts_the_client_quote_in_scope_for_the_charge() -> None:
    """R-9: the charge binds to the number this player's screen showed."""
    service = _StubSceneService(opening=_opening())
    client = _client(_StubContainer(story_scene_service=service))

    res = client.post(
        "/api/v1/characters/c1/story-scene", json={"quoted_price_cr": 9.5},
    )

    assert res.status_code == 201
    assert service.quotes == [9.5]


def test_open_still_works_with_no_body_at_all() -> None:
    """SC1-A clients (and self-host) post nothing; that must keep opening.

    All they give up is the price binding, which is exactly what a missing
    quote means everywhere else in the SPA.
    """
    service = _StubSceneService(opening=_opening())
    client = _client(_StubContainer(story_scene_service=service))

    res = client.post("/api/v1/characters/c1/story-scene")

    assert res.status_code == 201
    assert service.quotes == [None]


def test_open_rejects_a_negative_quote() -> None:
    service = _StubSceneService(opening=_opening())
    client = _client(_StubContainer(story_scene_service=service))

    res = client.post(
        "/api/v1/characters/c1/story-scene", json={"quoted_price_cr": -1},
    )

    assert res.status_code == 422
    assert service.opened_for == []


def test_open_maps_insufficient_credits_to_402() -> None:
    request = httpx.Request("POST", "https://user.example/charge")
    service = _StubSceneService(raises=ExpectedCloudRefusal(
        "402",
        request=request,
        response=httpx.Response(402, request=request),
        code=INSUFFICIENT_CREDITS_CODE,
        reason="螢火不足",
    ))
    client = _client(_StubContainer(story_scene_service=service))

    res = client.post("/api/v1/characters/c1/story-scene")

    assert res.status_code == 402
    assert res.json()["detail"]["code"] == INSUFFICIENT_CREDITS_CODE


def test_open_maps_a_moved_price_to_409_with_the_new_number() -> None:
    """Nothing was reserved, so the honest answer names the new price."""
    service = _StubSceneService(raises=ActionPriceChanged(
        action_key=ACTION_STORY_SCENE_OPEN,
        expected_price_cr=6.0,
        current_price_cr=8.0,
    ))
    client = _client(_StubContainer(story_scene_service=service))

    res = client.post(
        "/api/v1/characters/c1/story-scene", json={"quoted_price_cr": 6.0},
    )

    assert res.status_code == 409
    detail = res.json()["detail"]
    assert detail["code"] == "price_changed"
    assert detail["current_price_cr"] == 8.0


# ── state ────────────────────────────────────────────────────────────


def test_state_returns_the_open_session() -> None:
    service = _StubSceneService(current=_session())
    client = _client(_StubContainer(story_scene_service=service))

    res = client.get("/api/v1/characters/c1/story-scene")

    assert res.status_code == 200
    assert res.json()["status"] == "open"
    assert res.json()["conversation_id"] == "conv-1"


def test_state_returns_null_rather_than_404_when_no_scene_is_running() -> None:
    """A character that exists and is not in a scene is a 200/null, so the
    composer can tell it apart from a character that does not exist."""
    client = _client(_StubContainer(
        story_scene_service=_StubSceneService(current=None),
    ))

    res = client.get("/api/v1/characters/c1/story-scene")

    assert res.status_code == 200
    assert res.json() is None


def test_state_exposes_the_closed_shape() -> None:
    closed = _session().closed(reason=SCENE_CLOSE_MANUAL, at=NOW)
    client = _client(_StubContainer(
        story_scene_service=_StubSceneService(current=closed),
    ))

    body = client.get("/api/v1/characters/c1/story-scene").json()

    assert body["status"] == "closed"
    assert body["closed_reason"] == SCENE_CLOSE_MANUAL
    assert body["closed_at"] is not None


def test_state_on_a_foreign_character_is_404() -> None:
    client = _client(_StubContainer(
        story_scene_service=_StubSceneService(current=_session()),
        character_service=_StubCharacterService(None),
    ))

    assert client.get("/api/v1/characters/nope/story-scene").status_code == 404


# ── end (SC1-D) ──────────────────────────────────────────────────────


def _closing(*, narration: str | None = "她把譜收好，一個人待到天亮。") -> SceneClosing:
    closed = _session().closed(reason=SCENE_CLOSE_MANUAL, at=NOW)
    return SceneClosing(
        session=closed,
        closing_narration=(
            Message(
                role=MessageRole.ASSISTANT,
                content=narration,
                kind=MessageKind.SCENE_NARRATION,
                created_at=NOW,
            )
            if narration is not None else None
        ),
        canon_landed=True,
    )


@pytest.mark.parametrize(
    ("payload", "expected_session_id"),
    [({}, None), ({"session_id": "scene-1"}, "scene-1")],
    ids=["implicit", "explicit"],
)
def test_end_returns_the_closed_session_and_the_wrap_up(
    payload: dict, expected_session_id: str | None,
) -> None:
    service = _StubSceneService(current=_session(), closing=_closing())
    client = _client(_StubContainer(story_scene_service=service))

    res = client.post("/api/v1/characters/c1/story-scene/end", json=payload)

    assert res.status_code == 200
    body = res.json()
    assert set(body) == {"session", "closing_narration"}
    assert body["session"]["status"] == "closed"
    assert body["session"]["closed_reason"] == SCENE_CLOSE_MANUAL
    assert body["session"]["closed_at"] is not None
    # narration is the scene's own voice, not a chat bubble
    assert body["closing_narration"]["kind"] == "scene_narration"
    assert body["closing_narration"]["role"] == "assistant"
    # the body's session_id reaches the service verbatim rather than being
    # silently dropped — it is what stops a stale tab closing a new scene
    assert service.ended == [("c1", expected_session_id)]


def test_end_without_a_wrap_up_is_still_a_successful_close() -> None:
    """No prose is a success: the writer may be unwired, unavailable, or
    its draft refused for inventing player actions (§3.4 #1)."""
    client = _client(_StubContainer(
        story_scene_service=_StubSceneService(closing=_closing(narration=None)),
    ))

    res = client.post("/api/v1/characters/c1/story-scene/end", json={})

    assert res.status_code == 200
    assert res.json()["session"]["status"] == "closed"
    assert res.json()["closing_narration"] is None


@pytest.mark.parametrize(
    ("error", "expected_code"),
    [
        (SceneNotOpen("not in a scene"), "no_open_scene"),
        (SceneSessionMismatch("stale-1", "scene-2"), "scene_session_mismatch"),
        (ConversationBusyError("conv-1"), CONVERSATION_BUSY_CODE),
    ],
    ids=["no-scene", "stale-session", "busy"],
)
def test_end_refusals_are_structured_409s(
    error: Exception, expected_code: str,
) -> None:
    """409, not 404: the character exists and it is the client's idea of
    the *state* that is stale — re-reading GET /story-scene is the fix."""
    client = _client(_StubContainer(
        story_scene_service=_StubSceneService(end_raises=error),
    ))

    res = client.post("/api/v1/characters/c1/story-scene/end", json={})

    assert res.status_code == 409
    assert res.json()["detail"]["code"] == expected_code
    assert res.json()["detail"]["message"]


def test_end_on_a_foreign_character_is_404() -> None:
    service = _StubSceneService(closing=_closing())
    client = _client(_StubContainer(
        story_scene_service=service,
        character_service=_StubCharacterService(None),
    ))

    res = client.post("/api/v1/characters/nope/story-scene/end", json={})

    assert res.status_code == 404
    assert service.ended == []


def test_end_without_the_service_wired_is_503() -> None:
    client = _client(_StubContainer(story_scene_service=None))

    res = client.post("/api/v1/characters/c1/story-scene/end", json={})

    assert res.status_code == 503
