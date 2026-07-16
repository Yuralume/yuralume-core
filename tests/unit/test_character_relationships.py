from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kokoro_link.api.routes.character_relationships import router
from kokoro_link.application.services.active_llm_provider import (
    PreferenceBackedActiveLLMProvider,
)
from kokoro_link.application.services.character_encounter_service import (
    CharacterEncounterMemoryWriter,
    CharacterEncounterPlanner,
    CharacterEncounterRunner,
    CharacterEncounterService,
    EncounterReflection,
    _clean_generated_line,
)
from kokoro_link.application.services.character_relationship_service import (
    CharacterRelationshipService,
    CharacterRelationshipUpdate,
    CharacterRelationshipValidationError,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.character_encounter import CharacterEncounter
from kokoro_link.domain.entities.character_encounter_intent import (
    CharacterEncounterIntent,
)
from kokoro_link.domain.entities.schedule import DailySchedule, ScheduleActivity
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.memory_kind import MemoryKind
from kokoro_link.infrastructure.llm.fake import FakeChatModel
from kokoro_link.infrastructure.llm.registry import InMemoryChatModelRegistry
from kokoro_link.infrastructure.memory.in_memory import InMemoryMemoryRepository
from kokoro_link.infrastructure.repositories.in_memory_character_encounters import (
    InMemoryCharacterEncounterRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_character_encounter_intents import (
    InMemoryCharacterEncounterIntentRepository,
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
from kokoro_link.application.services.schedule_service import ScheduleService


class _Embedder:
    @property
    def is_operational(self) -> bool:
        return True

    async def embed(self, text: str):  # pragma: no cover - compatibility
        return (float(len(text) or 1), 0.5, 0.25)

    async def embed_many(self, texts):
        return [(float(index + 1), 0.5, 0.25) for index, _ in enumerate(texts)]


def _character(name: str) -> Character:
    return Character.create(
        name=name,
        summary=f"{name} 的摘要",
        personality=[],
        interests=[],
        speaking_style="自然",
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


@pytest.mark.asyncio
async def test_relationship_pair_dedupes_and_clamps_scores() -> None:
    characters = InMemoryCharacterRepository()
    repo = InMemoryCharacterRelationshipRepository()
    a = _character("A")
    b = _character("B")
    await characters.save(a)
    await characters.save(b)
    service = CharacterRelationshipService(
        repository=repo,
        character_repository=characters,
    )

    first = await service.create_or_enable(
        character_id=a.id,
        target_character_id=b.id,
        relationship_label="同學",
    )
    second = await service.create_or_enable(
        character_id=b.id,
        target_character_id=a.id,
    )
    updated = await service.update(
        first.id,
        CharacterRelationshipUpdate(
            affection_a_to_b=999,
            trust_b_to_a=-10,
            enabled=False,
        ),
    )

    assert second.id == first.id
    assert updated.affection_a_to_b == 100
    assert updated.trust_b_to_a == 0
    assert updated.enabled is False
    with pytest.raises(CharacterRelationshipValidationError):
        await service.create_or_enable(
            character_id=a.id,
            target_character_id=a.id,
        )


@pytest.mark.asyncio
async def test_planner_uses_only_enabled_pairs_and_writes_schedule_participants() -> None:
    characters = InMemoryCharacterRepository()
    relationship_repo = InMemoryCharacterRelationshipRepository()
    encounter_repo = InMemoryCharacterEncounterRepository()
    schedule_repo = InMemoryScheduleRepository()
    a = _character("A")
    b = _character("B")
    await characters.save(a)
    await characters.save(b)
    relationship_service = CharacterRelationshipService(
        repository=relationship_repo,
        character_repository=characters,
    )
    relationship = await relationship_service.create_or_enable(
        character_id=a.id,
        target_character_id=b.id,
    )
    await relationship_service.update(
        relationship.id,
        CharacterRelationshipUpdate(enabled=False),
    )
    now = datetime(2026, 5, 17, 9, 0, tzinfo=timezone.utc)
    await _seed_schedule(schedule_repo, a.id, now.date(), now)
    await _seed_schedule(schedule_repo, b.id, now.date(), now)
    schedule_service = ScheduleService(
        repository=schedule_repo,
        planner=NullSchedulePlanner(),
        local_tz=timezone.utc,
        conversation_repository=InMemoryConversationRepository(),
    )
    planner = CharacterEncounterPlanner(
        relationship_repository=relationship_repo,
        encounter_repository=encounter_repo,
        character_repository=characters,
        schedule_service=schedule_service,
        schedule_repository=schedule_repo,
        provider=_active_provider(),
        local_tz=timezone.utc,
    )

    assert await planner.plan_due(now=now) == []

    await relationship_service.update(
        relationship.id,
        CharacterRelationshipUpdate(enabled=True),
    )
    planned = await planner.plan_due(now=now)
    schedule = await schedule_repo.get(a.id, now.date())

    assert len(planned) == 1
    assert schedule is not None
    encounter_activity = schedule.activities[-1]
    assert encounter_activity.participant_refs[0].actor_kind == "character"
    assert encounter_activity.participant_refs[0].actor_id == b.id


@pytest.mark.asyncio
async def test_memory_writer_keeps_hearsay_separate_from_episodic() -> None:
    memory_repo = InMemoryMemoryRepository()
    writer = CharacterEncounterMemoryWriter(repository=memory_repo)
    a = _character("A")
    b = _character("B")
    encounter = CharacterEncounter.plan(
        relationship_id="rel-1",
        character_a_id=a.id,
        character_b_id=b.id,
        scheduled_for=datetime(2026, 5, 17, 9, 30, tzinfo=timezone.utc),
        location="咖啡廳",
        trigger_reason="自然碰面",
    )

    ids = await writer.write(
        encounter=encounter,
        char_a=a,
        char_b=b,
        transcript=(),
        reflection=EncounterReflection(
            summary_for_a="A 親眼和 B 在咖啡廳碰面。",
            summary_for_b="B 親眼和 A 在咖啡廳碰面。",
            hearsay_for_a=("B 說她覺得使用者最近很累。",),
        ),
    )
    memories_a = await memory_repo.list_all_for_character(a.id)
    hearsay = [item for item in memories_a if item.kind == MemoryKind.HEARSAY]

    assert len(ids) == 3
    assert any(item.kind == MemoryKind.EPISODIC for item in memories_a)
    assert hearsay
    assert hearsay[0].participants[0].role == "source"
    assert hearsay[0].participants[0].actor_id == b.id


@pytest.mark.asyncio
async def test_memory_writer_embeds_encounter_and_peer_fact_memories() -> None:
    memory_repo = InMemoryMemoryRepository()
    writer = CharacterEncounterMemoryWriter(
        repository=memory_repo,
        embedder=_Embedder(),
    )
    a = _character("A")
    b = _character("B")
    encounter = CharacterEncounter.plan(
        relationship_id="rel-1",
        character_a_id=a.id,
        character_b_id=b.id,
        scheduled_for=datetime(2026, 5, 17, 9, 30, tzinfo=timezone.utc),
        location="神社",
        trigger_reason="自然碰面",
    )

    ids = await writer.write(
        encounter=encounter,
        char_a=a,
        char_b=b,
        transcript=(),
        reflection=EncounterReflection(
            summary_for_a="A 親眼和 B 在神社碰面。",
            summary_for_b="B 親眼和 A 在神社碰面。",
            peer_facts_for_a=("B 在神社打工。",),
        ),
    )
    memories_a = await memory_repo.list_all_for_character(a.id)
    peer_facts = [
        item for item in memories_a
        if item.kind == MemoryKind.RELATIONSHIP
    ]

    assert len(ids) == 3
    assert all(item.embedding is not None for item in memories_a)
    assert all(item.tags_embedding is not None for item in memories_a)
    assert peer_facts
    assert peer_facts[0].participants[0].role == "peer"
    assert peer_facts[0].participants[0].actor_id == b.id
    assert f"peer:{b.id}" in peer_facts[0].tags


@pytest.mark.asyncio
async def test_runner_generates_short_pair_only_transcript_and_memories() -> None:
    characters = InMemoryCharacterRepository()
    relationship_repo = InMemoryCharacterRelationshipRepository()
    encounter_repo = InMemoryCharacterEncounterRepository()
    memory_repo = InMemoryMemoryRepository()
    a = _character("A")
    b = _character("B")
    await characters.save(a)
    await characters.save(b)
    relationship_service = CharacterRelationshipService(
        repository=relationship_repo,
        character_repository=characters,
    )
    relationship = await relationship_service.create_or_enable(
        character_id=a.id,
        target_character_id=b.id,
    )
    encounter = CharacterEncounter.plan(
        relationship_id=relationship.id,
        character_a_id=relationship.character_a_id,
        character_b_id=relationship.character_b_id,
        scheduled_for=datetime(2026, 5, 17, 9, 30, tzinfo=timezone.utc),
        location="公園",
        trigger_reason="自然碰面",
        max_turns=4,
    )
    await encounter_repo.save(encounter)
    runner = CharacterEncounterRunner(
        encounter_repository=encounter_repo,
        character_repository=characters,
        memory_writer=CharacterEncounterMemoryWriter(repository=memory_repo),
        relationship_service=relationship_service,
        provider=_active_provider(),
    )

    completed = await runner.run(encounter.id, now=encounter.scheduled_for)

    assert completed.status == "completed"
    assert 2 <= len(completed.transcript) <= 8
    assert {line.speaker_character_id for line in completed.transcript} == {a.id, b.id}
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


@pytest.mark.asyncio
async def test_encounter_service_split_run_and_plan_pending() -> None:
    characters = InMemoryCharacterRepository()
    relationship_repo = InMemoryCharacterRelationshipRepository()
    encounter_repo = InMemoryCharacterEncounterRepository()
    memory_repo = InMemoryMemoryRepository()
    schedule_repo = InMemoryScheduleRepository()
    a = _character("A")
    b = _character("B")
    await characters.save(a)
    await characters.save(b)
    relationship_service = CharacterRelationshipService(
        repository=relationship_repo,
        character_repository=characters,
    )
    relationship = await relationship_service.create_or_enable(
        character_id=a.id,
        target_character_id=b.id,
    )
    now = datetime(2026, 5, 17, 9, 0, tzinfo=timezone.utc)
    await _seed_schedule(schedule_repo, a.id, now.date(), now)
    await _seed_schedule(schedule_repo, b.id, now.date(), now)
    schedule_service = ScheduleService(
        repository=schedule_repo,
        planner=NullSchedulePlanner(),
        local_tz=timezone.utc,
        conversation_repository=InMemoryConversationRepository(),
    )
    service = CharacterEncounterService(
        planner=CharacterEncounterPlanner(
            relationship_repository=relationship_repo,
            encounter_repository=encounter_repo,
            character_repository=characters,
            schedule_service=schedule_service,
            schedule_repository=schedule_repo,
            provider=_active_provider(),
            local_tz=timezone.utc,
        ),
        runner=CharacterEncounterRunner(
            encounter_repository=encounter_repo,
            character_repository=characters,
            memory_writer=CharacterEncounterMemoryWriter(repository=memory_repo),
            relationship_service=relationship_service,
            provider=_active_provider(),
        ),
        encounter_repository=encounter_repo,
    )

    planned_result = await service.plan_pending(now=now)
    run_too_early = await service.run_pending(now=now)
    run_due = await service.run_pending(now=now + timedelta(hours=1))

    assert planned_result.planned == 1
    assert planned_result.completed == 0
    assert run_too_early.planned == 0
    assert run_too_early.completed == 0
    assert run_due.planned == 0
    assert run_due.completed == 1
    assert run_due.completed_ids == planned_result.planned_ids
    stored = await encounter_repo.get(planned_result.planned_ids[0])
    assert stored is not None
    assert stored.relationship_id == relationship.id


@pytest.mark.asyncio
async def test_encounter_keeps_max_turns_up_to_eight() -> None:
    encounter = CharacterEncounter.plan(
        relationship_id="rel-1",
        character_a_id="a",
        character_b_id="b",
        scheduled_for=datetime(2026, 5, 17, 9, 0, tzinfo=timezone.utc),
        location="公園",
        trigger_reason="有明確話題",
        max_turns=8,
    )

    assert encounter.max_turns == 8


def test_encounter_line_end_sentinel_is_protocol_not_dialogue_text() -> None:
    text, should_end = _clean_generated_line("今天就先聊到這裡。<END>")
    plain_text, plain_should_end = _clean_generated_line("今天就先聊到這裡。")

    assert text == "今天就先聊到這裡。"
    assert should_end is True
    assert plain_text == "今天就先聊到這裡。"
    assert plain_should_end is False


@pytest.mark.asyncio
async def test_planner_allows_second_pair_same_day_but_blocks_same_pair_gap() -> None:
    characters = InMemoryCharacterRepository()
    relationship_repo = InMemoryCharacterRelationshipRepository()
    encounter_repo = InMemoryCharacterEncounterRepository()
    schedule_repo = InMemoryScheduleRepository()
    a = _character("A")
    b = _character("B")
    c = _character("C")
    for character in (a, b, c):
        await characters.save(character)
    relationship_service = CharacterRelationshipService(
        repository=relationship_repo,
        character_repository=characters,
    )
    rel_ab = await relationship_service.create_or_enable(
        character_id=a.id,
        target_character_id=b.id,
    )
    rel_ac = await relationship_service.create_or_enable(
        character_id=a.id,
        target_character_id=c.id,
    )
    now = datetime(2026, 5, 17, 9, 0, tzinfo=timezone.utc)
    completed = CharacterEncounter.plan(
        relationship_id=rel_ab.id,
        character_a_id=rel_ab.character_a_id,
        character_b_id=rel_ab.character_b_id,
        scheduled_for=now - timedelta(hours=1),
        location="街角",
        trigger_reason="先前碰面",
    ).complete(
        transcript=(),
        summary_for_a="A 和 B 已碰面。",
        summary_for_b="B 和 A 已碰面。",
        memory_ids=(),
        at=now - timedelta(hours=1),
    )
    await encounter_repo.save(completed)
    await relationship_service.apply_reflection(
        rel_ab.id,
        interacted_at=now - timedelta(hours=1),
    )
    for character in (a, b, c):
        await _seed_schedule(schedule_repo, character.id, now.date(), now)
    schedule_service = ScheduleService(
        repository=schedule_repo,
        planner=NullSchedulePlanner(),
        local_tz=timezone.utc,
        conversation_repository=InMemoryConversationRepository(),
    )
    planner = CharacterEncounterPlanner(
        relationship_repository=relationship_repo,
        encounter_repository=encounter_repo,
        character_repository=characters,
        schedule_service=schedule_service,
        schedule_repository=schedule_repo,
        provider=_active_provider(),
        local_tz=timezone.utc,
    )

    planned = await planner.plan_due(now=now)

    assert {item.relationship_id for item in planned} == {rel_ac.id}


@pytest.mark.asyncio
async def test_planner_consumes_peer_meet_intent_and_uses_topic() -> None:
    characters = InMemoryCharacterRepository()
    relationship_repo = InMemoryCharacterRelationshipRepository()
    encounter_repo = InMemoryCharacterEncounterRepository()
    intent_repo = InMemoryCharacterEncounterIntentRepository()
    schedule_repo = InMemoryScheduleRepository()
    a = _character("芊芊")
    b = _character("小鈴")
    await characters.save(a)
    await characters.save(b)
    relationship_service = CharacterRelationshipService(
        repository=relationship_repo,
        character_repository=characters,
    )
    relationship = await relationship_service.create_or_enable(
        character_id=a.id,
        target_character_id=b.id,
    )
    now = datetime(2026, 5, 17, 9, 0, tzinfo=timezone.utc)
    tomorrow = now + timedelta(days=1)
    await _seed_schedule(schedule_repo, a.id, tomorrow.date(), tomorrow)
    await _seed_schedule(schedule_repo, b.id, tomorrow.date(), tomorrow)
    intent = CharacterEncounterIntent.create(
        character_id=a.id,
        peer_character_id=b.id,
        desired_after=tomorrow,
        topic="聊使用者交代的明天碰面",
        source_text="明天去找小鈴",
        now=now,
    )
    await intent_repo.add(intent)
    schedule_service = ScheduleService(
        repository=schedule_repo,
        planner=NullSchedulePlanner(),
        local_tz=timezone.utc,
        conversation_repository=InMemoryConversationRepository(),
    )
    planner = CharacterEncounterPlanner(
        relationship_repository=relationship_repo,
        encounter_repository=encounter_repo,
        character_repository=characters,
        schedule_service=schedule_service,
        schedule_repository=schedule_repo,
        provider=_active_provider(),
        local_tz=timezone.utc,
        intent_repository=intent_repo,
    )

    planned = await planner.plan_due(now=now)
    consumed = await intent_repo.get(intent.id)

    assert len(planned) == 1
    assert planned[0].relationship_id == relationship.id
    assert "聊使用者交代的明天碰面" in planned[0].trigger_reason
    assert planned[0].max_turns == 6
    assert planned[0].scheduled_for.date() == tomorrow.date()
    assert consumed is not None
    assert consumed.status == "consumed"


def test_relationship_routes_round_trip() -> None:
    characters = InMemoryCharacterRepository()
    relationship_repo = InMemoryCharacterRelationshipRepository()
    encounter_repo = InMemoryCharacterEncounterRepository()
    memory_repo = InMemoryMemoryRepository()
    schedule_repo = InMemoryScheduleRepository()
    a = _character("A")
    b = _character("B")

    async def _seed() -> None:
        await characters.save(a)
        await characters.save(b)

    import asyncio

    asyncio.run(_seed())
    relationship_service = CharacterRelationshipService(
        repository=relationship_repo,
        character_repository=characters,
    )
    schedule_service = ScheduleService(
        repository=schedule_repo,
        planner=NullSchedulePlanner(),
        local_tz=timezone.utc,
    )
    encounter_service = CharacterEncounterService(
        planner=CharacterEncounterPlanner(
            relationship_repository=relationship_repo,
            encounter_repository=encounter_repo,
            character_repository=characters,
            schedule_service=schedule_service,
            schedule_repository=schedule_repo,
            provider=_active_provider(),
            local_tz=timezone.utc,
        ),
        runner=CharacterEncounterRunner(
            encounter_repository=encounter_repo,
            character_repository=characters,
            memory_writer=CharacterEncounterMemoryWriter(repository=memory_repo),
            relationship_service=relationship_service,
            provider=_active_provider(),
        ),
        encounter_repository=encounter_repo,
    )
    app = FastAPI()
    app.state.container = SimpleNamespace(
        character_relationship_service=relationship_service,
        character_relationship_repository=relationship_repo,
        character_encounter_service=encounter_service,
    )
    app.include_router(router, prefix="/api/v1")
    client = TestClient(app)

    created = client.post(
        f"/api/v1/characters/{a.id}/relationships",
        json={"target_character_id": b.id, "relationship_label": "朋友"},
    )
    assert created.status_code == 201
    relationship_id = created.json()["id"]

    listed = client.get(f"/api/v1/characters/{a.id}/relationships")
    assert listed.status_code == 200
    assert listed.json()[0]["relationship_label"] == "朋友"

    patched = client.patch(
        f"/api/v1/character-relationships/{relationship_id}",
        json={"enabled": False, "trust_a_to_b": 90},
    )
    assert patched.status_code == 200
    assert patched.json()["enabled"] is False
    assert patched.json()["trust_a_to_b"] == 90


async def _seed_schedule(
    repo: InMemoryScheduleRepository,
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
