from __future__ import annotations

import asyncio
from dataclasses import dataclass

from fastapi import FastAPI
from fastapi.testclient import TestClient

from kokoro_link.api.dependencies import get_container, get_current_user_id
from kokoro_link.api.routes.arc_series import router
from kokoro_link.application.services.arc_series_service import ArcSeriesService
from kokoro_link.application.services.arc_template_intake_service import (
    BeatDraft,
    TemplateDraft,
)
from kokoro_link.domain.entities.arc_template import ArcTemplate, ArcTemplateBeat
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.repositories.in_memory_arc_series import (
    InMemoryArcSeriesRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_arc_templates import (
    InMemoryArcTemplateRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)


_TEST_USER_ID = "alice"


@dataclass
class _Container:
    arc_series_service: ArcSeriesService | None
    arc_series_continuation_draft_service: object | None = None


def _client(container: _Container) -> TestClient:
    app = FastAPI()
    app.dependency_overrides[get_container] = lambda: container
    app.dependency_overrides[get_current_user_id] = lambda: _TEST_USER_ID
    app.include_router(router, prefix="/api/v1")
    return TestClient(app)


def _template(template_id: str) -> ArcTemplate:
    return ArcTemplate.create(
        id=template_id,
        title=f"Template {template_id}",
        premise="一段可接續劇情。",
        theme="growth",
        duration_days=7,
        beats=[
            ArcTemplateBeat.create(
                sequence=0,
                day_offset=0,
                title="Opening",
                summary="故事開始。",
            ),
        ],
    )


def _character() -> Character:
    return Character.create(
        name="Aki",
        summary="",
        personality=[],
        interests=[],
        speaking_style="",
        boundaries=[],
        state=CharacterState(
            emotion="neutral",
            affection=50,
            fatigue=0,
            trust=50,
            energy=100,
        ),
        user_id=_TEST_USER_ID,
    )


@dataclass
class _Fixture:
    client: TestClient
    series_repo: InMemoryArcSeriesRepository
    character_repo: InMemoryCharacterRepository
    character: Character


def _fixture() -> _Fixture:
    series_repo = InMemoryArcSeriesRepository()
    template_repo = InMemoryArcTemplateRepository()
    character_repo = InMemoryCharacterRepository()
    character = _character()
    asyncio.run(template_repo.save_for_user(_template("book-one"), user_id=_TEST_USER_ID))
    asyncio.run(template_repo.save_for_user(_template("book-two"), user_id=_TEST_USER_ID))
    asyncio.run(character_repo.save(character))
    service = ArcSeriesService(
        series_repository=series_repo,
        template_repository=template_repo,
        character_repository=character_repo,
    )
    return _Fixture(
        client=_client(_Container(arc_series_service=service)),
        series_repo=series_repo,
        character_repo=character_repo,
        character=character,
    )


def _series_payload() -> dict[str, object]:
    return {
        "id": "series-a",
        "title": "連載篇",
        "premise": "兩本劇本依序展開。",
        "theme": "growth",
        "tone": "dramatic",
        "world_frames": ["modern"],
        "required_traits": ["student"],
        "template_ids": ["book-one", "book-two"],
    }


class _DraftService:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def draft_next_season(self, **kwargs):  # noqa: ANN003
        self.calls.append(kwargs)
        return TemplateDraft(
            id="next-season",
            title="Next Season",
            premise="A reviewable continuation draft.",
            theme="growth",
            beats=(
                BeatDraft(
                    sequence=0,
                    day_offset=0,
                    title="New Door",
                    summary="A concrete next-season opening.",
                ),
            ),
        )


def test_create_list_get_and_reorder_arc_series() -> None:
    fixture = _fixture()

    created = fixture.client.post("/api/v1/arc-series", json=_series_payload())
    assert created.status_code == 201
    assert created.json()["id"] == "series-a"
    assert created.json()["member_count"] == 2
    assert created.json()["binding"]["world_frames"] == ["modern"]

    listed = fixture.client.get("/api/v1/arc-series")
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()] == ["series-a"]

    fetched = fixture.client.get("/api/v1/arc-series/series-a")
    assert fetched.status_code == 200
    assert fetched.json()["members"][0]["template_id"] == "book-one"

    reordered = fixture.client.post(
        "/api/v1/arc-series/series-a/reorder",
        json={"template_ids": ["book-two", "book-one"]},
    )
    assert reordered.status_code == 200
    assert [item["template_id"] for item in reordered.json()["members"]] == [
        "book-two",
        "book-one",
    ]


def test_bind_progress_and_clear_arc_series_binding() -> None:
    fixture = _fixture()
    fixture.client.post("/api/v1/arc-series", json=_series_payload())

    bound = fixture.client.post(
        "/api/v1/arc-series/series-a/bind-to-character",
        json={"character_id": fixture.character.id},
    )
    assert bound.status_code == 200
    saved = asyncio.run(fixture.character_repo.get(fixture.character.id))
    assert saved is not None
    assert saved.arc_series_id == "series-a"

    progress = fixture.client.get(
        f"/api/v1/characters/{fixture.character.id}/arc-series-progress/series-a",
    )
    assert progress.status_code == 200
    assert progress.json()["current_index"] == 0
    assert progress.json()["status"] == "active"

    cleared = fixture.client.delete(
        f"/api/v1/characters/{fixture.character.id}/arc-series-binding",
    )
    assert cleared.status_code == 204
    saved = asyncio.run(fixture.character_repo.get(fixture.character.id))
    assert saved is not None
    assert saved.arc_series_id is None


def test_draft_next_season_returns_template_draft_payload() -> None:
    fixture = _fixture()
    draft_service = _DraftService()
    fixture.client.app.dependency_overrides[get_container] = lambda: _Container(
        arc_series_service=None,
        arc_series_continuation_draft_service=draft_service,
    )

    response = fixture.client.post(
        "/api/v1/arc-series/series-a/draft-next-season",
        json={
            "character_id": fixture.character.id,
            "instruction": "Keep it quiet.",
            "selected_memory_ids": ["mem-a"],
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["id"] == "next-season"
    assert data["beats"][0]["title"] == "New Door"
    assert draft_service.calls[0]["series_id"] == "series-a"
    assert draft_service.calls[0]["character_id"] == fixture.character.id
    assert draft_service.calls[0]["user_id"] == _TEST_USER_ID
    assert draft_service.calls[0]["instruction"] == "Keep it quiet."
    assert draft_service.calls[0]["selected_memory_ids"] == ["mem-a"]


def test_arc_series_routes_return_503_when_service_missing() -> None:
    client = _client(_Container(arc_series_service=None))

    response = client.get("/api/v1/arc-series")

    assert response.status_code == 503
