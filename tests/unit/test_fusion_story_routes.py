from __future__ import annotations

from dataclasses import dataclass

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
from kokoro_link.domain.entities.fusion_story import FusionStory


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
