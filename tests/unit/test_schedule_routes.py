"""Schedule activity CRUD routes.

Covers ``POST`` / ``PATCH`` / ``DELETE`` on individual activities —
thin delegates to ``ScheduleService.apply_adjustments`` with the
existing memorialized protection and overlap trimming.

The edit + delete routes intentionally return **current state** on
no-op results (memorialized activity / unknown id) so the UI always
has a consistent view to reconcile against, without the user having
to handle a separate 409 branch.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kokoro_link.api.dependencies import get_container
from kokoro_link.api.routes.schedule import router
from kokoro_link.application.dto.character import CharacterResponse
from kokoro_link.application.services.schedule_service import ScheduleService
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.schedule import DailySchedule, ScheduleActivity
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.repositories.in_memory_schedules import (
    InMemoryScheduleRepository,
)

UTC = timezone.utc


class _EmptyPlanner:
    """Planner stub that emits an empty day — lets ``ensure_schedule``
    lazy-create a blank schedule the operator can add activities to."""

    async def plan_day(
        self, *, character: Character, date_: date, local_tz,
        recent_dialogue_summary: str = "",
        **_: object,
    ) -> DailySchedule:
        return DailySchedule.create(
            character_id=character.id, date_=date_, activities=[],
        )


class _StubCharacterService:
    def __init__(self, character: Character | None) -> None:
        self._character = character

    async def get_character(self, character_id: str):
        if self._character is None or self._character.id != character_id:
            return None
        return CharacterResponse.from_domain(self._character)

    async def get_character_entity(self, character_id: str) -> Character | None:
        if self._character is None or self._character.id != character_id:
            return None
        return self._character


@dataclass
class _StubContainer:
    schedule_service: ScheduleService
    character_service: _StubCharacterService = field(
        default_factory=lambda: _StubCharacterService(None),
    )


def _build_client(container: _StubContainer) -> TestClient:
    app = FastAPI()
    app.state.container = container
    app.dependency_overrides[get_container] = lambda: container
    app.include_router(router, prefix="/api/v1")
    return TestClient(app)


def _character(id_: str = "c1") -> Character:
    return Character.create(
        name="Mio",
        summary="",
        personality=[],
        interests=[],
        speaking_style="",
        boundaries=[],
        state=CharacterState(emotion="neutral", affection=50, fatigue=0, trust=50, energy=100),
    ).with_id(id_) if hasattr(Character, "with_id") else Character(
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


def _service(target: date) -> tuple[ScheduleService, InMemoryScheduleRepository]:
    repo = InMemoryScheduleRepository()
    svc = ScheduleService(repository=repo, planner=_EmptyPlanner(), local_tz=UTC)
    return svc, repo


# ---- Add ----

def test_add_activity_lazy_creates_day_then_inserts() -> None:
    target = date(2026, 4, 21)
    svc, _ = _service(target)
    char = _character()
    client = _build_client(_StubContainer(
        schedule_service=svc, character_service=_StubCharacterService(char),
    ))

    res = client.post(
        f"/api/v1/characters/c1/schedule/activities?date={target.isoformat()}",
        json={
            "start": "10:00", "end": "11:30",
            "description": "寫歌詞", "category": "creative",
            "busy_score": 0.7,
        },
    )

    assert res.status_code == 201
    body = res.json()
    assert len(body["activities"]) == 1
    activity = body["activities"][0]
    assert activity["description"] == "寫歌詞"
    assert activity["category"] == "creative"
    assert activity["busy_score"] == pytest.approx(0.7)
    assert activity["has_memory"] is False


def test_add_activity_rejects_reversed_time_range() -> None:
    target = date(2026, 4, 21)
    svc, _ = _service(target)
    char = _character()
    client = _build_client(_StubContainer(
        schedule_service=svc, character_service=_StubCharacterService(char),
    ))

    res = client.post(
        f"/api/v1/characters/c1/schedule/activities?date={target.isoformat()}",
        json={
            "start": "12:00", "end": "10:00",
            "description": "bad", "category": "x",
        },
    )

    assert res.status_code == 400


def test_add_activity_404_on_unknown_character() -> None:
    target = date(2026, 4, 21)
    svc, _ = _service(target)
    client = _build_client(_StubContainer(
        schedule_service=svc, character_service=_StubCharacterService(None),
    ))

    res = client.post(
        f"/api/v1/characters/ghost/schedule/activities?date={target.isoformat()}",
        json={
            "start": "10:00", "end": "11:00",
            "description": "x", "category": "y",
        },
    )

    assert res.status_code == 404


def test_add_activity_rejects_bad_hhmm() -> None:
    target = date(2026, 4, 21)
    svc, _ = _service(target)
    char = _character()
    client = _build_client(_StubContainer(
        schedule_service=svc, character_service=_StubCharacterService(char),
    ))

    res = client.post(
        f"/api/v1/characters/c1/schedule/activities?date={target.isoformat()}",
        json={
            "start": "25:00", "end": "26:00",
            "description": "x", "category": "y",
        },
    )

    assert res.status_code == 422  # pydantic validation


# ---- Update ----

@pytest.mark.asyncio
async def test_update_activity_modifies_fields() -> None:
    target = date(2026, 4, 21)
    svc, repo = _service(target)
    char = _character()
    # Seed a schedule with one activity.
    activity = ScheduleActivity.create(
        start_at=datetime(2026, 4, 21, 9, 0, tzinfo=UTC),
        end_at=datetime(2026, 4, 21, 10, 0, tzinfo=UTC),
        description="old", category="misc",
    )
    schedule = DailySchedule.create(
        character_id="c1", date_=target, activities=[activity],
    )
    await repo.save(schedule)
    client = _build_client(_StubContainer(
        schedule_service=svc, character_service=_StubCharacterService(char),
    ))

    res = client.patch(
        f"/api/v1/characters/c1/schedule/activities/{activity.id}"
        f"?date={target.isoformat()}",
        json={"description": "new", "busy_score": 0.9},
    )

    assert res.status_code == 200
    body = res.json()
    edited = next(a for a in body["activities"] if a["id"] == activity.id)
    assert edited["description"] == "new"
    assert edited["busy_score"] == pytest.approx(0.9)
    # Untouched fields preserved.
    assert edited["category"] == "misc"


@pytest.mark.asyncio
async def test_update_memorialized_activity_is_noop_returns_current() -> None:
    """Memorialized blocks are history — service-layer guard keeps them
    immutable. Route should respond with current state (no 409) so the
    UI can show the rejection visually instead of handling it."""
    target = date(2026, 4, 21)
    svc, repo = _service(target)
    char = _character()
    activity = ScheduleActivity.create(
        start_at=datetime(2026, 4, 21, 9, 0, tzinfo=UTC),
        end_at=datetime(2026, 4, 21, 10, 0, tzinfo=UTC),
        description="locked", category="misc",
    ).with_memorialized(True)
    schedule = DailySchedule.create(
        character_id="c1", date_=target, activities=[activity],
    )
    await repo.save(schedule)
    client = _build_client(_StubContainer(
        schedule_service=svc, character_service=_StubCharacterService(char),
    ))

    res = client.patch(
        f"/api/v1/characters/c1/schedule/activities/{activity.id}"
        f"?date={target.isoformat()}",
        json={"description": "hijack"},
    )

    assert res.status_code == 200
    body = res.json()
    kept = next(a for a in body["activities"] if a["id"] == activity.id)
    assert kept["description"] == "locked"


# ---- Delete ----

@pytest.mark.asyncio
async def test_delete_activity_removes_it() -> None:
    target = date(2026, 4, 21)
    svc, repo = _service(target)
    char = _character()
    activity = ScheduleActivity.create(
        start_at=datetime(2026, 4, 21, 9, 0, tzinfo=UTC),
        end_at=datetime(2026, 4, 21, 10, 0, tzinfo=UTC),
        description="doomed", category="misc",
    )
    schedule = DailySchedule.create(
        character_id="c1", date_=target, activities=[activity],
    )
    await repo.save(schedule)
    client = _build_client(_StubContainer(
        schedule_service=svc, character_service=_StubCharacterService(char),
    ))

    res = client.delete(
        f"/api/v1/characters/c1/schedule/activities/{activity.id}"
        f"?date={target.isoformat()}",
    )

    assert res.status_code == 200
    body = res.json()
    assert body["activities"] == []


@pytest.mark.asyncio
async def test_delete_memorialized_activity_noop_returns_current() -> None:
    target = date(2026, 4, 21)
    svc, repo = _service(target)
    char = _character()
    activity = ScheduleActivity.create(
        start_at=datetime(2026, 4, 21, 9, 0, tzinfo=UTC),
        end_at=datetime(2026, 4, 21, 10, 0, tzinfo=UTC),
        description="done", category="misc",
    ).with_memorialized(True)
    schedule = DailySchedule.create(
        character_id="c1", date_=target, activities=[activity],
    )
    await repo.save(schedule)
    client = _build_client(_StubContainer(
        schedule_service=svc, character_service=_StubCharacterService(char),
    ))

    res = client.delete(
        f"/api/v1/characters/c1/schedule/activities/{activity.id}"
        f"?date={target.isoformat()}",
    )

    assert res.status_code == 200
    body = res.json()
    assert len(body["activities"]) == 1
    assert body["activities"][0]["id"] == activity.id
