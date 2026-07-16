"""ProactiveScheduler background character encounter tick coverage."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import pytest

from kokoro_link.application.services.active_llm_provider import (
    PreferenceBackedActiveLLMProvider,
)
from kokoro_link.application.services.character_encounter_service import (
    CharacterEncounterMemoryWriter,
    CharacterEncounterPlanner,
    CharacterEncounterRunner,
    CharacterEncounterService,
)
from kokoro_link.application.services.character_relationship_service import (
    CharacterRelationshipService,
)
from kokoro_link.application.services.proactive_scheduler import ProactiveScheduler
from kokoro_link.application.services.schedule_memorializer import ScheduleMemorializer
from kokoro_link.application.services.schedule_service import ScheduleService
from kokoro_link.contracts.clock import ensure_utc
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.schedule import DailySchedule, ScheduleActivity
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.memory_kind import MemoryKind
from kokoro_link.domain.value_objects.proactive_trigger import ProactiveTrigger
from kokoro_link.infrastructure.llm.fake import FakeChatModel
from kokoro_link.infrastructure.llm.registry import InMemoryChatModelRegistry
from kokoro_link.infrastructure.memory.in_memory import InMemoryMemoryRepository
from kokoro_link.infrastructure.repositories.in_memory_character_encounters import (
    InMemoryCharacterEncounterRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_character_relationships import (
    InMemoryCharacterRelationshipRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_conversations import (
    InMemoryConversationRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_preferences import (
    InMemoryPreferencesRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_schedules import (
    InMemoryScheduleRepository,
)
from kokoro_link.infrastructure.schedule.null_planner import NullSchedulePlanner
from tests.unit._messaging_harness import build_messaging_harness, create_character


@dataclass(slots=True)
class _FrozenClock:
    value: datetime

    def now(self) -> datetime:
        return ensure_utc(self.value)


class _RecordingDispatcher:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ProactiveTrigger]] = []

    async def evaluate(
        self,
        *,
        character_id: str,
        trigger: ProactiveTrigger,
        now: datetime | None = None,  # noqa: ARG002
    ) -> None:
        self.calls.append((character_id, trigger))


class _RecordingEncounterService:
    def __init__(self, *, crash: bool = False) -> None:
        self.run_nows: list[datetime | None] = []
        self.plan_nows: list[datetime | None] = []
        self._crash = crash

    async def run_pending(self, *, now: datetime | None = None):
        self.run_nows.append(now)
        if self._crash:
            raise RuntimeError("encounter run exploded")
        return None

    async def plan_pending(self, *, now: datetime | None = None):
        self.plan_nows.append(now)
        if self._crash:
            raise RuntimeError("encounter plan exploded")
        return None


class _RecordingPeerKnowledgeService:
    def __init__(self, *, crash: bool = False) -> None:
        self.calls = 0
        self._crash = crash

    async def consolidate_due(self):
        self.calls += 1
        if self._crash:
            raise RuntimeError("peer knowledge exploded")
        from kokoro_link.application.services.character_social_knowledge_service import (
            PeerKnowledgeTickResult,
        )
        return PeerKnowledgeTickResult(consolidated=1)


def _character(name: str) -> Character:
    return Character.create(
        name=name,
        summary=f"{name} summary",
        personality=[],
        interests=[],
        speaking_style="natural",
        boundaries=[],
        state=CharacterState(
            emotion="neutral",
            affection=50,
            fatigue=0,
            trust=50,
            energy=100,
        ),
    )


def _active_provider() -> PreferenceBackedActiveLLMProvider:
    registry = InMemoryChatModelRegistry(default_provider_id="fake")
    registry.register(FakeChatModel(provider_id="fake"))
    return PreferenceBackedActiveLLMProvider(
        registry=registry,
        preferences=InMemoryPreferencesRepository(),
        default_provider_id="fake",
    )


async def _enable_proactive(harness, character_id: str) -> str:
    entity = await harness.character_repository.get(character_id)
    assert entity is not None
    updated = entity.update(
        name=None,
        summary=None,
        personality=None,
        interests=None,
        speaking_style=None,
        boundaries=None,
        state=None,
        aspirations=None,
        appearance=None,
        proactive_enabled=True,
    )
    await harness.character_repository.save(updated)
    return updated.id


async def _seed_low_busy_schedule(
    repo: InMemoryScheduleRepository,
    *,
    character_id: str,
    target: date,
    now: datetime,
) -> None:
    await repo.save(
        DailySchedule.create(
            character_id=character_id,
            date_=target,
            activities=[
                ScheduleActivity.create(
                    start_at=now,
                    end_at=now + timedelta(hours=2),
                    description="自由活動",
                    category="leisure",
                    location="街角",
                    busy_score=0.2,
                ),
            ],
        ),
    )


@pytest.mark.asyncio
async def test_tick_runs_encounters_every_tick_and_plans_on_interval() -> None:
    harness = build_messaging_harness()
    dto = await create_character(harness, name="Aki")
    await _enable_proactive(harness, dto.id)
    dispatcher = _RecordingDispatcher()
    encounters = _RecordingEncounterService()
    first_now = datetime(2026, 6, 18, 9, 0, tzinfo=timezone.utc)
    clock = _FrozenClock(first_now)
    scheduler = ProactiveScheduler(
        dispatcher=dispatcher,  # type: ignore[arg-type]
        character_repository=harness.character_repository,
        startup_grace_seconds=0.0,
        character_encounter_service=encounters,  # type: ignore[arg-type]
        encounter_plan_interval_seconds=1800.0,
        clock=clock,
    )

    await scheduler._tick_all()  # noqa: SLF001 - focused scheduler contract.
    clock.value = first_now + timedelta(minutes=5)
    await scheduler._tick_all()  # noqa: SLF001 - focused scheduler contract.
    clock.value = first_now + timedelta(minutes=31)
    await scheduler._tick_all()  # noqa: SLF001 - focused scheduler contract.

    assert encounters.run_nows == [
        first_now,
        first_now + timedelta(minutes=5),
        first_now + timedelta(minutes=31),
    ]
    assert encounters.plan_nows == [
        first_now,
        first_now + timedelta(minutes=31),
    ]


@pytest.mark.asyncio
async def test_encounter_crash_does_not_break_tick_sweep() -> None:
    harness = build_messaging_harness()
    dto = await create_character(harness, name="Aki")
    character_id = await _enable_proactive(harness, dto.id)
    dispatcher = _RecordingDispatcher()
    scheduler = ProactiveScheduler(
        dispatcher=dispatcher,  # type: ignore[arg-type]
        character_repository=harness.character_repository,
        startup_grace_seconds=0.0,
        character_encounter_service=_RecordingEncounterService(crash=True),  # type: ignore[arg-type]
    )

    await scheduler._tick_all()  # noqa: SLF001 - focused fail-soft contract.

    assert any(
        cid == character_id and trigger == ProactiveTrigger.TICK
        for cid, trigger in dispatcher.calls
    )


@pytest.mark.asyncio
async def test_tick_consolidates_peer_knowledge_on_interval() -> None:
    harness = build_messaging_harness()
    dto = await create_character(harness, name="Aki")
    await _enable_proactive(harness, dto.id)
    dispatcher = _RecordingDispatcher()
    peer_knowledge = _RecordingPeerKnowledgeService()
    first_now = datetime(2026, 6, 18, 9, 0, tzinfo=timezone.utc)
    clock = _FrozenClock(first_now)
    scheduler = ProactiveScheduler(
        dispatcher=dispatcher,  # type: ignore[arg-type]
        character_repository=harness.character_repository,
        startup_grace_seconds=0.0,
        character_social_knowledge_service=peer_knowledge,  # type: ignore[arg-type]
        peer_knowledge_interval_seconds=1800.0,
        clock=clock,
    )

    await scheduler._tick_all()  # noqa: SLF001 - focused scheduler contract.
    clock.value = first_now + timedelta(minutes=5)
    await scheduler._tick_all()  # noqa: SLF001 - focused scheduler contract.
    clock.value = first_now + timedelta(minutes=31)
    await scheduler._tick_all()  # noqa: SLF001 - focused scheduler contract.

    assert peer_knowledge.calls == 2


@pytest.mark.asyncio
async def test_peer_knowledge_crash_does_not_break_tick_sweep() -> None:
    harness = build_messaging_harness()
    dto = await create_character(harness, name="Aki")
    character_id = await _enable_proactive(harness, dto.id)
    dispatcher = _RecordingDispatcher()
    scheduler = ProactiveScheduler(
        dispatcher=dispatcher,  # type: ignore[arg-type]
        character_repository=harness.character_repository,
        startup_grace_seconds=0.0,
        character_social_knowledge_service=_RecordingPeerKnowledgeService(crash=True),  # type: ignore[arg-type]
    )

    await scheduler._tick_all()  # noqa: SLF001 - focused fail-soft contract.

    assert any(
        cid == character_id and trigger == ProactiveTrigger.TICK
        for cid, trigger in dispatcher.calls
    )


@pytest.mark.asyncio
async def test_scheduler_background_encounter_smoke_writes_rich_memory_only() -> None:
    characters = InMemoryCharacterRepository()
    relationship_repo = InMemoryCharacterRelationshipRepository()
    encounter_repo = InMemoryCharacterEncounterRepository()
    schedule_repo = InMemoryScheduleRepository()
    memory_repo = InMemoryMemoryRepository()
    a = _character("A")
    b = _character("B")
    await characters.save(a)
    await characters.save(b)
    relationship_service = CharacterRelationshipService(
        repository=relationship_repo,
        character_repository=characters,
    )
    await relationship_service.create_or_enable(
        character_id=a.id,
        target_character_id=b.id,
    )
    now = datetime(2026, 6, 18, 9, 0, tzinfo=timezone.utc)
    await _seed_low_busy_schedule(
        schedule_repo,
        character_id=a.id,
        target=now.date(),
        now=now,
    )
    await _seed_low_busy_schedule(
        schedule_repo,
        character_id=b.id,
        target=now.date(),
        now=now,
    )
    schedule_service = ScheduleService(
        repository=schedule_repo,
        planner=NullSchedulePlanner(),
        local_tz=timezone.utc,
        conversation_repository=InMemoryConversationRepository(),
    )
    active_provider = _active_provider()
    encounter_service = CharacterEncounterService(
        planner=CharacterEncounterPlanner(
            relationship_repository=relationship_repo,
            encounter_repository=encounter_repo,
            character_repository=characters,
            schedule_service=schedule_service,
            schedule_repository=schedule_repo,
            provider=active_provider,
            local_tz=timezone.utc,
        ),
        runner=CharacterEncounterRunner(
            encounter_repository=encounter_repo,
            character_repository=characters,
            memory_writer=CharacterEncounterMemoryWriter(repository=memory_repo),
            relationship_service=relationship_service,
            provider=active_provider,
        ),
        encounter_repository=encounter_repo,
    )
    memorializer = ScheduleMemorializer(
        schedule_repository=schedule_repo,
        memory_repository=memory_repo,
        local_tz=timezone.utc,
    )
    clock = _FrozenClock(now)
    scheduler = ProactiveScheduler(
        dispatcher=_RecordingDispatcher(),  # type: ignore[arg-type]
        character_repository=characters,
        startup_grace_seconds=0.0,
        schedule_service=schedule_service,
        schedule_memorializer=memorializer,
        character_encounter_service=encounter_service,
        clock=clock,
    )

    await scheduler._tick_all()  # noqa: SLF001 - background plan.
    clock.value = now + timedelta(hours=1)
    await scheduler._tick_all()  # noqa: SLF001 - background run + skip generic memory.

    memories_a = await memory_repo.list_all_for_character(a.id)
    memories_b = await memory_repo.list_all_for_character(b.id)
    assert {memory.kind for memory in memories_a} == {
        MemoryKind.EPISODIC,
        MemoryKind.RELATIONSHIP,
    }
    assert {memory.kind for memory in memories_b} == {
        MemoryKind.EPISODIC,
        MemoryKind.RELATIONSHIP,
    }
    assert all("encounter" in memory.tags for memory in memories_a)
    assert all("schedule" not in memory.tags for memory in memories_a)
    assert all("encounter" in memory.tags for memory in memories_b)
    assert all("schedule" not in memory.tags for memory in memories_b)
    schedule_a = await schedule_repo.get(a.id, now.date())
    assert schedule_a is not None
    encounter_blocks = [
        activity for activity in schedule_a.activities
        if any(ref.role == "encounter_partner" for ref in activity.participant_refs)
    ]
    assert encounter_blocks
    assert all(activity.memorialized for activity in encounter_blocks)


@pytest.mark.asyncio
async def test_no_encounter_service_keeps_legacy_behavior() -> None:
    harness = build_messaging_harness()
    dto = await create_character(harness, name="Aki")
    character_id = await _enable_proactive(harness, dto.id)
    dispatcher = _RecordingDispatcher()
    scheduler = ProactiveScheduler(
        dispatcher=dispatcher,  # type: ignore[arg-type]
        character_repository=harness.character_repository,
        startup_grace_seconds=0.0,
    )

    await scheduler._tick_all()  # noqa: SLF001 - focused legacy contract.

    assert any(
        cid == character_id and trigger == ProactiveTrigger.TICK
        for cid, trigger in dispatcher.calls
    )
