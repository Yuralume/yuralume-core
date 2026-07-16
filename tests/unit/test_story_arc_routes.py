"""Smoke-level BDD for the story arc REST routes.

Thin wrappers over ``StoryArcService`` — we don't need to duplicate all
service-level cases here, just confirm the HTTP glue (status codes,
response shape, character-not-found branch) works end to end.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kokoro_link.api.dependencies import get_container
from kokoro_link.api.routes.story_arc import router
from kokoro_link.application.services.story_arc_service import StoryArcService
from kokoro_link.contracts.story_arc import StoryArcPlannerPort
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.story_arc import (
    StoryArc,
    StoryArcBeat,
    TENSION_SETUP,
)
from kokoro_link.domain.entities.story_event import StoryEvent
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.repositories.in_memory_story_arcs import (
    InMemoryStoryArcRepository,
)


class _FixedPlanner(StoryArcPlannerPort):
    async def plan_arc(
        self, *, character, start_date, duration_days=21, beat_count_hint=5, hint=None,
        recent_dialogue_summary: str = "",
    ) -> StoryArc:
        arc = StoryArc.create(
            character_id=character.id,
            title=hint or "planned arc",
            premise="premise",
            theme="custom",
            start_date=start_date,
            end_date=start_date + timedelta(days=duration_days),
        )
        beat = StoryArcBeat.create(
            arc_id=arc.id, sequence=0,
            scheduled_date=start_date,
            title="first", summary="the first beat",
            tension=TENSION_SETUP,
        )
        return arc.with_beats([beat])


class _StubCharacterService:
    def __init__(self, character: Character | None) -> None:
        self._character = character

    async def get_character_entity(self, character_id: str) -> Character | None:
        if self._character is None or self._character.id != character_id:
            return None
        return self._character


class _StubScheduleService:
    def __init__(self, today: date) -> None:
        self.today = today
        self.calls: list[str] = []

    async def today_for_character(self, character: Character) -> date:
        self.calls.append(character.id)
        return self.today


@dataclass
class _StubContainer:
    story_arc_service: StoryArcService | None
    character_service: _StubCharacterService = field(
        default_factory=lambda: _StubCharacterService(None),
    )
    schedule_service: _StubScheduleService | None = None
    story_beat_scene_service: object | None = None


class _StubBeatSceneService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    async def play_beat(
        self,
        character: Character,
        *,
        beat_id: str,
        user_involvement_policy: str | None = None,
        **kwargs,
    ) -> StoryEvent:
        self.calls.append((beat_id, user_involvement_policy))
        return StoryEvent.create(
            character_id=character.id,
            date="2026-06-01",
            arc_beat_id=beat_id,
            narrative="我把這場戲完整演完了。",
            emotional_tone="content",
        )


def _make_character(id_: str = "c1") -> Character:
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
        state=CharacterState(emotion="neutral", affection=50, fatigue=0, trust=50, energy=100),
    )


def _build_client(container: _StubContainer) -> TestClient:
    app = FastAPI()
    app.state.container = container
    app.dependency_overrides[get_container] = lambda: container
    app.include_router(router, prefix="/api/v1")
    return TestClient(app)


def _service() -> StoryArcService:
    return StoryArcService(
        repository=InMemoryStoryArcRepository(),
        planner=_FixedPlanner(),
    )


def test_start_arc_returns_201_and_body_shape() -> None:
    service = _service()
    char = _make_character()
    client = _build_client(_StubContainer(
        story_arc_service=service,
        character_service=_StubCharacterService(char),
    ))

    res = client.post(
        "/api/v1/characters/c1/story-arcs",
        json={"hint": "試鏡的一段時間", "duration_days": 14, "beat_count": 3},
    )

    assert res.status_code == 201
    body = res.json()
    assert body["title"] == "試鏡的一段時間"
    assert body["status"] == "active"
    assert body["character_id"] == "c1"
    assert len(body["beats"]) >= 1


def test_start_arc_uses_owner_local_today() -> None:
    service = _service()
    char = _make_character()
    schedule_service = _StubScheduleService(date(2026, 6, 15))
    client = _build_client(_StubContainer(
        story_arc_service=service,
        character_service=_StubCharacterService(char),
        schedule_service=schedule_service,
    ))

    res = client.post("/api/v1/characters/c1/story-arcs", json={})

    assert res.status_code == 201
    body = res.json()
    assert schedule_service.calls == ["c1"]
    assert body["start_date"] == "2026-06-15"
    assert body["beats"][0]["scheduled_date"] == "2026-06-15"


def test_start_arc_accepts_three_day_duration() -> None:
    """duration_days floor is 3 (relaxed from 7), matching _MIN_BEATS."""
    service = _service()
    char = _make_character()
    client = _build_client(_StubContainer(
        story_arc_service=service,
        character_service=_StubCharacterService(char),
    ))

    res = client.post(
        "/api/v1/characters/c1/story-arcs",
        json={"hint": "短篇試驗", "duration_days": 3, "beat_count": 3},
    )

    assert res.status_code == 201


def test_start_arc_rejects_duration_below_three() -> None:
    client = _build_client(_StubContainer(
        story_arc_service=_service(),
        character_service=_StubCharacterService(_make_character()),
    ))

    res = client.post(
        "/api/v1/characters/c1/story-arcs",
        json={"duration_days": 2, "beat_count": 3},
    )

    assert res.status_code == 422  # pydantic validation


def test_start_arc_rejects_duration_shorter_than_beat_count() -> None:
    """Cross-field: duration_days must be >= beat_count (one beat per real day)."""
    client = _build_client(_StubContainer(
        story_arc_service=_service(),
        character_service=_StubCharacterService(_make_character()),
    ))

    res = client.post(
        "/api/v1/characters/c1/story-arcs",
        json={"duration_days": 3, "beat_count": 5},
    )

    assert res.status_code == 422


def test_start_arc_accepts_seven_and_ninety_day_boundaries() -> None:
    service = _service()
    char = _make_character()
    client = _build_client(_StubContainer(
        story_arc_service=service,
        character_service=_StubCharacterService(char),
    ))

    res_low = client.post(
        "/api/v1/characters/c1/story-arcs",
        json={"hint": "一週", "duration_days": 7, "beat_count": 5},
    )
    assert res_low.status_code == 201

    res_high = client.post(
        "/api/v1/characters/c1/story-arcs",
        json={"hint": "長篇", "duration_days": 90, "beat_count": 7},
    )
    assert res_high.status_code == 201


def test_start_arc_404_when_character_missing() -> None:
    client = _build_client(_StubContainer(
        story_arc_service=_service(),
        character_service=_StubCharacterService(None),
    ))

    res = client.post(
        "/api/v1/characters/ghost/story-arcs", json={},
    )

    assert res.status_code == 404


def test_get_active_returns_null_when_no_arc() -> None:
    char = _make_character()
    client = _build_client(_StubContainer(
        story_arc_service=_service(),
        character_service=_StubCharacterService(char),
    ))

    res = client.get("/api/v1/characters/c1/story-arcs/active")

    assert res.status_code == 200
    assert res.json() is None


def test_service_unavailable_when_service_not_configured() -> None:
    client = _build_client(_StubContainer(
        story_arc_service=None,
        character_service=_StubCharacterService(_make_character()),
    ))

    res = client.get("/api/v1/characters/c1/story-arcs")

    assert res.status_code == 503


def test_update_beat_404_for_unknown_id() -> None:
    client = _build_client(_StubContainer(
        story_arc_service=_service(),
        character_service=_StubCharacterService(_make_character()),
    ))

    res = client.patch(
        "/api/v1/story-arc-beats/nope",
        json={"title": "x"},
    )

    assert res.status_code == 404


def test_simulate_beat_returns_story_event() -> None:
    service = _service()
    char = _make_character()
    scene_service = _StubBeatSceneService()
    client = _build_client(_StubContainer(
        story_arc_service=service,
        character_service=_StubCharacterService(char),
        story_beat_scene_service=scene_service,
    ))
    started = client.post("/api/v1/characters/c1/story-arcs", json={}).json()
    beat_id = started["beats"][0]["id"]

    res = client.post(
        f"/api/v1/story-arc-beats/{beat_id}/simulate",
        json={"user_involvement_policy": "使用者不在場，請自主演完。"},
    )

    assert res.status_code == 200
    body = res.json()
    assert body["arc_beat_id"] == beat_id
    assert body["narrative"] == "我把這場戲完整演完了。"
    assert scene_service.calls == [(beat_id, "使用者不在場，請自主演完。")]


def test_simulate_beat_503_when_scene_service_missing() -> None:
    service = _service()
    char = _make_character()
    client = _build_client(_StubContainer(
        story_arc_service=service,
        character_service=_StubCharacterService(char),
    ))
    started = client.post("/api/v1/characters/c1/story-arcs", json={}).json()
    beat_id = started["beats"][0]["id"]

    res = client.post(f"/api/v1/story-arc-beats/{beat_id}/simulate", json={})

    assert res.status_code == 503


@pytest.mark.asyncio
async def test_full_crud_roundtrip() -> None:
    """Start → add beat → patch beat → delete beat → list → abandon."""
    service = _service()
    char = _make_character()
    client = _build_client(_StubContainer(
        story_arc_service=service,
        character_service=_StubCharacterService(char),
    ))

    # Start
    started = client.post(
        "/api/v1/characters/c1/story-arcs", json={},
    ).json()
    arc_id = started["id"]

    # Add beat
    added = client.post(
        f"/api/v1/story-arcs/{arc_id}/beats",
        json={
            "scheduled_date": (date.today() + timedelta(days=5)).isoformat(),
            "title": "extra", "summary": "something new",
            "tension": "rising",
        },
    ).json()
    extra_beat = next(b for b in added["beats"] if b["title"] == "extra")

    # Patch beat
    patched = client.patch(
        f"/api/v1/story-arc-beats/{extra_beat['id']}",
        json={"title": "edited"},
    ).json()
    assert any(b["title"] == "edited" for b in patched["beats"])

    # Delete beat
    deleted = client.delete(
        f"/api/v1/story-arc-beats/{extra_beat['id']}",
    ).json()
    assert not any(b["title"] == "edited" for b in deleted["beats"])

    # Abandon
    abandoned = client.post(
        f"/api/v1/story-arcs/{arc_id}/abandon",
    ).json()
    assert abandoned["status"] == "abandoned"
