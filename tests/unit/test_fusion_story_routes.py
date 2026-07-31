from __future__ import annotations

from dataclasses import dataclass

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from kokoro_link.api.dependencies import get_container, get_current_user_id
from kokoro_link.api.routes.fusion_story import router
from kokoro_link.application.dto.fusion_story import CreateFusionStoryRequest
from kokoro_link.application.services.arc_template_intake_service import (
    BeatDraft,
    TemplateDraft,
)
from kokoro_link.contracts.cloud_action_billing import (
    ACTION_FUSION_STORY,
    ACTION_FUSION_STORY_ITERATE,
    ActionPriceChanged,
    client_quoted_price,
)
from kokoro_link.domain.entities.fusion_story import FusionStory
from kokoro_link.infrastructure.llm.cloud_refusal import (
    INSUFFICIENT_CREDITS_CODE,
    ExpectedCloudRefusal,
)


_TEST_USER_ID = "alice"


def _ready_story() -> FusionStory:
    return FusionStory.create_pending(
        id="fusion-1",
        character_ids=["c-a", "c-b"],
        prompt="A finished story.",
    ).with_full_text("Finished prose.")


def _draft() -> TemplateDraft:
    return TemplateDraft(
        id="promise_arc",
        title="Promise Arc",
        premise="A playable arc about trust returning through small scenes.",
        theme="friendship",
        tone="daily",
        duration_days=7,
        world_frames=("modern",),
        beats=(
            BeatDraft(
                sequence=0,
                day_offset=0,
                title="First Step",
                summary="The character chooses whether to ask about the promise.",
            ),
        ),
    )


@dataclass
class _FusionStoryServiceStub:
    story: FusionStory | None = None

    async def get(self, story_id: str) -> FusionStory | None:
        assert story_id == "fusion-1"
        return self.story


class _CreateFusionStoryServiceStub:
    """Records ``create`` calls and echoes a pending story back.

    Only the create endpoint uses this stub; it lets the route tests
    assert the schema-accepted cast reaches the service unchanged."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def create(self, **kwargs) -> FusionStory:
        self.calls.append(kwargs)
        return FusionStory.create_pending(
            id="fusion-new",
            character_ids=list(kwargs["character_ids"]),
            prompt=kwargs["prompt"],
        )


class _AdaptServiceStub:
    def __init__(
        self,
        *,
        draft: TemplateDraft | None = None,
        error: ValueError | None = None,
    ) -> None:
        self.draft = draft
        self.error = error
        self.calls: list[dict] = []

    async def adapt(self, story_id: str, **kwargs) -> TemplateDraft | None:
        self.calls.append({"story_id": story_id, **kwargs})
        if self.error is not None:
            raise self.error
        return self.draft


@dataclass
class _Container:
    fusion_story_service: _FusionStoryServiceStub | None
    fusion_to_arc_draft_service: _AdaptServiceStub | None
    character_service = None
    operator_profile_service = None


def _client(container: _Container) -> TestClient:
    app = FastAPI()
    app.dependency_overrides[get_container] = lambda: container
    app.dependency_overrides[get_current_user_id] = lambda: _TEST_USER_ID
    app.include_router(router, prefix="/api/v1")
    return TestClient(app)


def test_adapt_to_arc_returns_template_draft_payload() -> None:
    adapter = _AdaptServiceStub(draft=_draft())
    client = _client(_Container(_FusionStoryServiceStub(_ready_story()), adapter))

    response = client.post(
        "/api/v1/fusion-stories/fusion-1/adapt-to-arc",
        json={"instruction": "Keep it quiet."},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == "promise_arc"
    assert body["beats"][0]["title"] == "First Step"
    assert adapter.calls[0]["user_id"] == _TEST_USER_ID
    assert adapter.calls[0]["instruction"] == "Keep it quiet."


def test_adapt_to_arc_maps_not_ready_to_409() -> None:
    adapter = _AdaptServiceStub(error=ValueError("Fusion story is not ready"))
    client = _client(_Container(_FusionStoryServiceStub(_ready_story()), adapter))

    response = client.post("/api/v1/fusion-stories/fusion-1/adapt-to-arc")

    assert response.status_code == 409


def test_adapt_to_arc_maps_fail_soft_none_to_503() -> None:
    adapter = _AdaptServiceStub(draft=None)
    client = _client(_Container(_FusionStoryServiceStub(_ready_story()), adapter))

    response = client.post("/api/v1/fusion-stories/fusion-1/adapt-to-arc")

    assert response.status_code == 503


def test_create_accepts_single_character_returns_202() -> None:
    # C1-5: a solo cast is accepted at the route + schema layer and the
    # single id reaches the service unchanged.
    service = _CreateFusionStoryServiceStub()
    client = _client(_Container(service, None))

    response = client.post(
        "/api/v1/fusion-stories",
        json={"character_ids": ["c-a"], "prompt": "獨角戲"},
    )

    assert response.status_code == 202
    assert service.calls[0]["character_ids"] == ["c-a"]
    assert response.json()["character_ids"] == ["c-a"]


def test_create_rejects_empty_cast_returns_422() -> None:
    # An empty cast is rejected at the schema edge (min_length=1) before
    # the service is ever invoked.
    service = _CreateFusionStoryServiceStub()
    client = _client(_Container(service, None))

    response = client.post(
        "/api/v1/fusion-stories",
        json={"character_ids": [], "prompt": "獨角戲"},
    )

    assert response.status_code == 422
    assert service.calls == []


def test_create_request_schema_accepts_single_and_rejects_empty() -> None:
    ok = CreateFusionStoryRequest(character_ids=["c-a"], prompt="p")
    assert ok.character_ids == ["c-a"]
    with pytest.raises(ValidationError):
        CreateFusionStoryRequest(character_ids=[], prompt="p")


# --- AP2: the billing refusals have to beat the 202 -------------------------


class _RefusingCreateServiceStub:
    """A create that fails at the charge, before any story exists.

    That is the whole point of charging at the entry point: the player is told
    "top up" / "the price moved" while they are still looking at the form,
    instead of watching a story spin for a minute and land on ``failed``.
    """

    def __init__(self, error: Exception) -> None:
        self._error = error
        self.quotes: list[float | None] = []

    async def create(self, **kwargs):  # noqa: ANN003, ARG002
        self.quotes.append(client_quoted_price(ACTION_FUSION_STORY))
        raise self._error


def test_create_maps_insufficient_credits_to_402() -> None:
    request = httpx.Request("POST", "https://user.example/charge")
    service = _RefusingCreateServiceStub(
        ExpectedCloudRefusal(
            "402",
            request=request,
            response=httpx.Response(402, request=request),
            code=INSUFFICIENT_CREDITS_CODE,
            reason="螢火不足",
        ),
    )
    client = _client(_Container(service, None))  # type: ignore[arg-type]

    response = client.post(
        "/api/v1/fusion-stories",
        json={"character_ids": ["c-a"], "prompt": "獨角戲"},
    )

    assert response.status_code == 402
    assert response.json()["detail"]["code"] == INSUFFICIENT_CREDITS_CODE


def test_create_maps_a_moved_price_to_409_with_the_new_number() -> None:
    service = _RefusingCreateServiceStub(
        ActionPriceChanged(
            action_key=ACTION_FUSION_STORY,
            expected_price_cr=12.0,
            current_price_cr=15.0,
        ),
    )
    client = _client(_Container(service, None))  # type: ignore[arg-type]

    response = client.post(
        "/api/v1/fusion-stories",
        json={"character_ids": ["c-a"], "prompt": "獨角戲", "quoted_price_cr": 12.0},
    )

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "price_changed"
    assert detail["current_price_cr"] == 15.0


def test_create_puts_the_client_quote_in_scope_for_the_charge() -> None:
    """R9: the body's quote is what the charge binds to, not a server cache."""
    service = _RefusingCreateServiceStub(ValueError("stop here"))
    client = _client(_Container(service, None))  # type: ignore[arg-type]

    client.post(
        "/api/v1/fusion-stories",
        json={"character_ids": ["c-a"], "prompt": "獨角戲", "quoted_price_cr": 12.5},
    )

    assert service.quotes == [12.5]


def test_polish_forwards_its_optional_quote_and_still_accepts_no_body() -> None:
    class _PolishStub(_FusionStoryServiceStub):
        def __init__(self, story: FusionStory) -> None:
            super().__init__(story)
            self.quotes: list[float | None] = []

        async def iterate_polish(self, story_id: str, **kwargs):  # noqa: ANN003, ARG002
            self.quotes.append(
                client_quoted_price(ACTION_FUSION_STORY_ITERATE),
            )
            return self.story

    service = _PolishStub(_ready_story())
    client = _client(_Container(service, None))  # type: ignore[arg-type]

    assert client.post("/api/v1/fusion-stories/fusion-1/polish").status_code == 202
    assert client.post(
        "/api/v1/fusion-stories/fusion-1/polish", json={"quoted_price_cr": 3.0},
    ).status_code == 202
    assert service.quotes == [None, 3.0]
