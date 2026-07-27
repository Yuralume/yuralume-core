from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.application.services.character_encounter_service import EncounterTickResult
from kokoro_link.application.services.character_social_knowledge_service import (
    PeerKnowledgeTickResult,
)
from kokoro_link.application.services.proactive_scheduler import ProactiveScheduler
from kokoro_link.application.services.beat_due_checker import BeatScanResult
from kokoro_link.contracts.background_activity_gate import BackgroundActivityClass
from kokoro_link.domain.entities.character import Character, CharacterState
from kokoro_link.domain.value_objects.account_runtime_profile import AccountRuntimeProfile
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)


class _NoopDispatcher:
    async def evaluate(self, **kwargs) -> None:
        return None


class _ProfileResolver:
    def __init__(self, *, background: int) -> None:
        self.profile = AccountRuntimeProfile(
            name="plus",
            background_activity_multiplier=background,
        )

    async def resolve_for_operator(self, operator_id: str) -> AccountRuntimeProfile:
        return self.profile


@dataclass
class _Clock:
    value: datetime

    def now(self) -> datetime:
        return self.value


class _Feed:
    def __init__(self) -> None:
        self.calls = 0

    async def tick(self, character) -> None:
        self.calls += 1


class _Encounters:
    def __init__(self, character: Character) -> None:
        self.character = character
        self.run_nows: list[datetime] = []
        self.plan_nows: list[datetime] = []

    async def run_pending(self, *, now, gate) -> EncounterTickResult:
        if await gate.allows(
            self.character, BackgroundActivityClass.ENCOUNTER_RUN,
        ):
            self.run_nows.append(now)
        return EncounterTickResult()

    async def plan_pending(self, *, now, gate) -> EncounterTickResult:
        if await gate.allows(
            self.character, BackgroundActivityClass.ENCOUNTER_PLAN,
        ):
            self.plan_nows.append(now)
        return EncounterTickResult()


class _PeerKnowledge:
    def __init__(self, character: Character) -> None:
        self.character = character
        self.calls = 0

    async def consolidate_due(self, *, gate) -> PeerKnowledgeTickResult:
        if await gate.allows(
            self.character, BackgroundActivityClass.PEER_KNOWLEDGE,
        ):
            self.calls += 1
        return PeerKnowledgeTickResult()


class _Dream:
    def __init__(self) -> None:
        self.calls = 0

    async def should_run_now(self, *args, **kwargs) -> bool:
        return True

    async def run_consolidation(self, *args, **kwargs) -> None:
        self.calls += 1


class _DreamRepository:
    def __init__(self, character: Character) -> None:
        self.character = character

    async def list_characters_with_pending(self):
        return [(self.character.id, self.character.user_id)]


class _CountingTick:
    def __init__(self) -> None:
        self.calls = 0

    async def tick(self, *args, **kwargs) -> None:
        self.calls += 1


class _Schedule:
    def __init__(self) -> None:
        self.calls = 0

    async def ensure_window(self, character) -> None:
        self.calls += 1


class _Memorializer:
    def __init__(self) -> None:
        self.calls = 0

    async def memorialize(self, **kwargs) -> None:
        self.calls += 1


class _BeatChecker:
    def __init__(self) -> None:
        self.calls = 0

    async def scan(self, character, *, now) -> BeatScanResult:
        self.calls += 1
        return BeatScanResult.empty()


class _RestRecovery:
    def __init__(self) -> None:
        self.calls = 0

    async def refresh(self, character, *, now) -> None:
        self.calls += 1


async def _stored_character(repository: InMemoryCharacterRepository) -> Character:
    character = Character.create(
        name="Airi",
        summary="",
        user_id="operator-1",
        personality=[],
        interests=[],
        speaking_style="",
        boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
        proactive_enabled=False,
    )
    await repository.save(character)
    return character


@pytest.mark.asyncio
async def test_background_gate_covers_cost_paths_but_not_reactive_or_daily_paths() -> None:
    repository = InMemoryCharacterRepository()
    character = await _stored_character(repository)
    feed = _Feed()
    encounters = _Encounters(character)
    peer = _PeerKnowledge(character)
    dream = _Dream()
    comment = _CountingTick()
    pending = _CountingTick()
    schedule = _Schedule()
    memorializer = _Memorializer()
    beat = _BeatChecker()
    rest = _RestRecovery()
    scheduler = ProactiveScheduler(
        dispatcher=_NoopDispatcher(),  # type: ignore[arg-type]
        character_repository=repository,
        account_runtime_profile_resolver=_ProfileResolver(background=3),
        feed_composer=feed,  # type: ignore[arg-type]
        feed_comment_reply=comment,  # type: ignore[arg-type]
        pending_follow_up_dispatcher=pending,  # type: ignore[arg-type]
        character_encounter_service=encounters,  # type: ignore[arg-type]
        encounter_plan_interval_seconds=0,
        character_social_knowledge_service=peer,  # type: ignore[arg-type]
        peer_knowledge_interval_seconds=0,
        persona_dream_service=dream,  # type: ignore[arg-type]
        persona_dream_repository=_DreamRepository(character),  # type: ignore[arg-type]
        schedule_service=schedule,  # type: ignore[arg-type]
        schedule_memorializer=memorializer,  # type: ignore[arg-type]
        beat_due_checker=beat,  # type: ignore[arg-type]
        rest_recovery_refresher=rest,  # type: ignore[arg-type]
        startup_grace_seconds=0,
    )

    for _ in range(3):
        await scheduler._tick_all()  # noqa: SLF001

    assert feed.calls == 1
    assert len(encounters.run_nows) == 1
    assert len(encounters.plan_nows) == 1
    assert peer.calls == 1
    assert dream.calls == 1
    assert comment.calls == 3
    assert pending.calls == 3
    assert schedule.calls == 3
    assert memorializer.calls == 3
    assert beat.calls == 3
    assert rest.calls == 3


class _GetRaisesRepository(InMemoryCharacterRepository):
    """list_active works normally; direct get() simulates a transient DB error."""

    async def get(self, character_id: str):
        raise RuntimeError("transient lookup failure")


@pytest.mark.asyncio
async def test_persona_dream_lookup_failure_fails_open() -> None:
    """The pre-gate contract (formerly _is_frozen) was explicit: a transient
    repo error must never silently suppress legitimate dream work. Lookup
    failure skips only the cost gate, not the pass itself."""
    repository = _GetRaisesRepository()
    character = await _stored_character(repository)
    dream = _Dream()
    scheduler = ProactiveScheduler(
        dispatcher=_NoopDispatcher(),  # type: ignore[arg-type]
        character_repository=repository,
        account_runtime_profile_resolver=_ProfileResolver(background=1),
        persona_dream_service=dream,  # type: ignore[arg-type]
        persona_dream_repository=_DreamRepository(character),  # type: ignore[arg-type]
        startup_grace_seconds=0,
    )

    await scheduler._tick_all()  # noqa: SLF001

    assert dream.calls == 1


@pytest.mark.asyncio
async def test_encounter_real_time_throttle_and_tick_gate_take_stricter_phase() -> None:
    repository = InMemoryCharacterRepository()
    character = await _stored_character(repository)
    start = datetime(2026, 7, 18, 0, 0, tzinfo=timezone.utc)
    clock = _Clock(start)
    encounters = _Encounters(character)
    scheduler = ProactiveScheduler(
        dispatcher=_NoopDispatcher(),  # type: ignore[arg-type]
        character_repository=repository,
        account_runtime_profile_resolver=_ProfileResolver(background=2),
        character_encounter_service=encounters,  # type: ignore[arg-type]
        encounter_plan_interval_seconds=1800,
        startup_grace_seconds=0,
        clock=clock,
    )

    for tick in range(8):
        clock.value = start + timedelta(minutes=tick * 5)
        await scheduler._tick_all()  # noqa: SLF001

    assert encounters.run_nows == [
        start + timedelta(minutes=5),
        start + timedelta(minutes=15),
        start + timedelta(minutes=25),
        start + timedelta(minutes=35),
    ]
    assert encounters.plan_nows == [
        start + timedelta(minutes=5),
        start + timedelta(minutes=35),
    ]
