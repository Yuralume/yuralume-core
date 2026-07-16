"""BDD coverage for chat promises becoming character encounters."""

from datetime import date, datetime, timedelta, timezone

import pytest

from kokoro_link.application.services.character_encounter_service import (
    CharacterEncounterPlanner,
)
from kokoro_link.application.services.character_relationship_service import (
    CharacterRelationshipService,
)
from kokoro_link.application.services.chat_service import ChatService
from kokoro_link.application.services.schedule_service import ScheduleService
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.schedule import DailySchedule, ScheduleActivity
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.llm.fake import FakeChatModel
from kokoro_link.infrastructure.llm.registry import InMemoryChatModelRegistry
from kokoro_link.infrastructure.post_turn.llm_processor import _parse_response
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
from kokoro_link.application.services.active_llm_provider import (
    PreferenceBackedActiveLLMProvider,
)


class _Clock:
    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now


def _character(name: str) -> Character:
    return Character.create(
        name=name,
        summary=f"{name} 的摘要",
        personality=["重視承諾"],
        interests=["日常聊天"],
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
async def test_chat_agreement_becomes_planned_peer_encounter() -> None:
    now = datetime(2026, 5, 17, 9, 0, tzinfo=timezone.utc)
    desired_after = now + timedelta(days=1)
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
    await _seed_schedule(schedule_repo, a.id, desired_after.date(), desired_after)
    await _seed_schedule(schedule_repo, b.id, desired_after.date(), desired_after)
    post_turn = _parse_response(
        f"""
        {{"memories": [], "state": null, "schedule_adjustments": [],
         "arc_adjustments": [], "message_promises": [],
         "peer_meet_intents": [{{
           "peer_name": "小鈴",
           "desired_after_iso": "{desired_after.date().isoformat()}",
           "topic": "聊使用者交代的明天碰面",
           "source_text": "明天去找小鈴"
         }}]}}
        """,
        character_id=a.id,
        conversation_id="conv-1",
        known_peer_lines=[f"- id={b.id} | name=小鈴"],
    )
    chat_service = ChatService.__new__(ChatService)
    chat_service._character_encounter_intent_repository = intent_repo  # noqa: SLF001
    chat_service._clock = _Clock(now)  # noqa: SLF001
    await chat_service._persist_peer_meet_intents(  # noqa: SLF001
        character_id=a.id,
        intents=post_turn.peer_meet_intents,
    )
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
    pending = await intent_repo.list_pending_for_character(a.id, now=now)

    assert len(planned) == 1
    assert planned[0].relationship_id == relationship.id
    assert planned[0].scheduled_for.date() == desired_after.date()
    assert planned[0].max_turns == 6
    assert "聊使用者交代的明天碰面" in planned[0].trigger_reason
    assert pending == []


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
