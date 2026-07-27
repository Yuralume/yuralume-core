from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import MethodType, SimpleNamespace

import pytest

from kokoro_link.application.services.character_encounter_service import (
    _GATE_DEFER_SECONDS,
    CharacterEncounterPlanner,
    CharacterEncounterRunner,
)
from kokoro_link.contracts.background_activity_gate import BackgroundActivityClass
from kokoro_link.domain.entities.character import Character, CharacterState
from kokoro_link.domain.entities.character_encounter import CharacterEncounter

_NOW = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)


def _character(character_id: str, *, user_id: str = "operator-1") -> Character:
    return Character(
        id=character_id,
        name=character_id,
        summary="",
        user_id=user_id,
        personality=[],
        interests=[],
        speaking_style="",
        boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
    )


def _encounter(index: int) -> CharacterEncounter:
    return CharacterEncounter(
        id=f"enc-{index}",
        relationship_id="rel-1",
        character_a_id="a",
        character_b_id="b",
        # Oldest-first ordering mirrors the SQL window; all already due.
        scheduled_for=_NOW - timedelta(hours=2) + timedelta(seconds=index),
        location="公園",
    )


class _EncounterRepository:
    def __init__(self, encounters: list[CharacterEncounter]) -> None:
        self.encounters = encounters
        self.limits: list[int] = []
        self.saved: list[CharacterEncounter] = []

    async def list_runnable(self, now, *, limit=20):
        self.limits.append(limit)
        return self.encounters[:limit]

    async def save(self, encounter: CharacterEncounter) -> None:
        self.saved.append(encounter)


class _CharacterRepository:
    def __init__(self, characters: dict[str, Character]) -> None:
        self.characters = characters

    async def get(self, character_id: str) -> Character | None:
        return self.characters.get(character_id)


class _Gate:
    """Allows only the character ids in ``allowed_ids`` (None = allow all)."""

    def __init__(self, allowed_ids: set[str] | None) -> None:
        self.allowed_ids = allowed_ids
        self.checked: list[tuple[str, BackgroundActivityClass]] = []

    async def allows(self, character, activity_class) -> bool:
        self.checked.append((character.id, activity_class))
        if self.allowed_ids is None:
            return True
        return character.id in self.allowed_ids


def _runner(
    repository: _EncounterRepository,
    characters: dict[str, Character],
) -> CharacterEncounterRunner:
    return CharacterEncounterRunner(
        encounter_repository=repository,  # type: ignore[arg-type]
        character_repository=_CharacterRepository(characters),  # type: ignore[arg-type]
        memory_writer=None,  # type: ignore[arg-type]
        relationship_service=None,  # type: ignore[arg-type]
        provider=None,  # type: ignore[arg-type]
    )


def _patch_run(runner: CharacterEncounterRunner, processed: list[str]) -> None:
    async def _run(self, encounter_id: str, *, now=None):
        processed.append(encounter_id)
        return SimpleNamespace(id=encounter_id, status="completed")

    runner.run = MethodType(_run, runner)  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_gated_encounters_are_deferred_not_left_at_queue_head() -> None:
    """Gate denial must re-anchor scheduled_for past *now* so stale rows stop
    outranking other operators' due work in the shared oldest-first window
    (cross-tenant starvation fix), while staying deferred-not-skipped."""
    characters = {"a": _character("a"), "b": _character("b")}
    encounters = [_encounter(index) for index in range(25)]
    repository = _EncounterRepository(encounters)
    runner = _runner(repository, characters)
    processed: list[str] = []
    _patch_run(runner, processed)

    denied = _Gate(allowed_ids=set())
    result = await runner.run_due(now=_NOW, gate=denied)

    assert result == ([], [])
    assert processed == []
    # Every fetched (top-20) gated row was pushed one tick forward.
    assert [item.id for item in repository.saved] == [
        f"enc-{index}" for index in range(20)
    ]
    expected = _NOW + timedelta(seconds=_GATE_DEFER_SECONDS)
    assert all(item.scheduled_for == expected for item in repository.saved)
    assert repository.limits == [20]


@pytest.mark.asyncio
async def test_allowed_gate_processes_bounded_batch() -> None:
    characters = {"a": _character("a"), "b": _character("b")}
    encounters = [_encounter(index) for index in range(25)]
    repository = _EncounterRepository(encounters)
    runner = _runner(repository, characters)
    processed: list[str] = []
    _patch_run(runner, processed)

    allowed = _Gate(allowed_ids=None)
    result = await runner.run_due(now=_NOW, gate=allowed)

    assert processed == [f"enc-{index}" for index in range(20)]
    assert result == (processed, [])
    assert repository.saved == []
    assert {c for _, c in allowed.checked} == {
        BackgroundActivityClass.ENCOUNTER_RUN,
    }


class _NoPendingEncounterRepository:
    async def has_pending_for_relationship(self, relationship_id: str) -> bool:
        return False


class _RecordingScheduleService:
    def __init__(self) -> None:
        self.calls = 0

    async def ensure_window(self, character, **kwargs):
        self.calls += 1
        raise RuntimeError("stop after gate — deeper planning not under test")


def _planner(
    characters: dict[str, Character],
    schedule_service: _RecordingScheduleService,
    relationship,
) -> CharacterEncounterPlanner:
    class _Relationships:
        async def list_enabled(self):
            return [relationship]

    return CharacterEncounterPlanner(
        relationship_repository=_Relationships(),  # type: ignore[arg-type]
        encounter_repository=_NoPendingEncounterRepository(),  # type: ignore[arg-type]
        character_repository=_CharacterRepository(characters),  # type: ignore[arg-type]
        schedule_service=schedule_service,  # type: ignore[arg-type]
        schedule_repository=None,  # type: ignore[arg-type]
        provider=None,  # type: ignore[arg-type]
        local_tz=timezone.utc,
    )


@pytest.mark.asyncio
async def test_plan_gate_requires_both_sides_and_short_circuits_before_planning() -> None:
    characters = {"a": _character("a"), "b": _character("b")}
    relationship = SimpleNamespace(
        id="rel-1",
        character_a_id="a",
        character_b_id="b",
        last_interaction_at=None,
    )
    schedule_service = _RecordingScheduleService()
    planner = _planner(characters, schedule_service, relationship)

    only_a = _Gate(allowed_ids={"a"})
    planned = await planner.plan_due(now=_NOW, gate=only_a)

    assert planned == []
    # Denied via char_b before any schedule-window (LLM-adjacent) work ran.
    assert schedule_service.calls == 0
    assert [cid for cid, _ in only_a.checked] == ["a", "b"]

    both = _Gate(allowed_ids={"a", "b"})
    await planner.plan_due(now=_NOW, gate=both)
    # Both sides consulted and the flow proceeded past the gate.
    assert [cid for cid, _ in both.checked] == ["a", "b"]
    assert schedule_service.calls >= 1


@pytest.mark.asyncio
async def test_run_gate_requires_both_sides_of_the_pair() -> None:
    """a/b slot assignment is lexicographic, so a single-sided check would
    make idle downshift a coin flip per relationship — both must be on-phase."""
    characters = {"a": _character("a"), "b": _character("b")}
    repository = _EncounterRepository([_encounter(0)])
    runner = _runner(repository, characters)
    processed: list[str] = []
    _patch_run(runner, processed)

    only_a = _Gate(allowed_ids={"a"})
    result = await runner.run_due(now=_NOW, gate=only_a)

    assert processed == []
    assert result == ([], [])
    # Denied via char_b → deferred like any gated row.
    assert [item.id for item in repository.saved] == ["enc-0"]
