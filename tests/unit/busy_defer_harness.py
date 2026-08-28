"""Shared wiring for the busy-defer / deferred-reply tests.

Two suites need the same stand-ins — the busy-defer branch of
:class:`ChatService` (`test_chat_service_busy_defer.py`) and the
turn-undo rollback of the rows that branch writes
(`test_turn_undo_follow_ups.py`). They are one harness rather than two
copies so a change in what the chat path actually does cannot leave one
suite testing a fiction of the other.

Nothing here asserts. Every class is the smallest stand-in that lets the
real ``ChatService`` run: a schedule service that answers with a fixed
activity, a decider that reads from a script, a spy that records what was
handed to the distributed release enqueuer.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

from kokoro_link.application.services.character_service import CharacterService
from kokoro_link.application.services.chat_service import ChatService
from kokoro_link.contracts.busy_reply_decider import BusyDecision
from kokoro_link.domain.entities.character_operator_relationship_seed import (
    CharacterOperatorRelationshipSeed,
)
from kokoro_link.domain.entities.operator_persona import (
    InteractionStrength,
    OperatorPersona,
)
from kokoro_link.domain.entities.operator_profile import OperatorProfile
from kokoro_link.domain.entities.schedule import ScheduleActivity
from kokoro_link.domain.value_objects.familiarity import Familiarity
from kokoro_link.infrastructure.llm.fake import FakeChatModel
from kokoro_link.infrastructure.llm.registry import InMemoryChatModelRegistry
from kokoro_link.infrastructure.memory.in_memory import InMemoryMemoryRepository
from kokoro_link.infrastructure.post_turn.null_processor import (
    NullPostTurnProcessor,
)
from kokoro_link.infrastructure.prompt.default import DefaultPromptContextBuilder
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_conversations import (
    InMemoryConversationRepository,
)
from kokoro_link.infrastructure.state.simple import SimpleStateEngine


def busy_activity(busy: float = 0.9) -> ScheduleActivity:
    now = datetime.now(timezone.utc)
    return ScheduleActivity.create(
        start_at=now - timedelta(minutes=30),
        end_at=now + timedelta(minutes=30),
        description="跟客戶開會",
        category="meeting",
        busy_score=busy,
    )


class StubScheduleService:
    """Minimal stand-in: ``ensure_schedule`` returns a sentinel,
    ``resolve_current`` returns whatever current_activity we configured."""

    def __init__(self, *, current_activity: ScheduleActivity | None) -> None:
        self.current_activity = current_activity

    async def ensure_schedule(self, character):
        # Truthy placeholder carrying the ``.date`` attribute the chat
        # path reads when threading the pending-invite / upcoming window.
        return SimpleNamespace(date=datetime.now(timezone.utc).date())

    def resolve_current(self, schedule, *, now=None):
        return self.current_activity, [], None

    def resolve_completed_today(
        self, schedule, *, now=None, local_tz=None, limit=8,
    ):
        return []

    def resolve_pending_invites_from_schedules(
        self, schedules, *, now=None, limit=1,
    ):
        return []

    async def get_schedule(self, character_id, *, date_=None):
        return None

    async def current_activity_response(self, character):  # pragma: no cover
        return None


class ScriptedDecider:
    def __init__(self, decisions: list[BusyDecision]) -> None:
        self.decisions = decisions
        self.calls: list[dict[str, Any]] = []

    async def decide(
        self,
        *,
        character,
        user_message,
        current_activity,
        recent_dialogue_summary=None,
        recent_proactive_attempts=(),
        relationship_context_lines=(),
        interaction_context_lines=(),
        now,
        local_tz=None,
        operator_primary_language="zh-TW",
    ):
        self.calls.append({
            "user_message": user_message,
            "current_activity": current_activity,
            "local_tz": local_tz,
            "recent_proactive_attempts": recent_proactive_attempts,
            "relationship_context_lines": relationship_context_lines,
            "interaction_context_lines": interaction_context_lines,
            "operator_primary_language": operator_primary_language,
        })
        if not self.decisions:
            return BusyDecision()
        return self.decisions.pop(0)


class StubOperatorProfileService:
    async def get_current(self) -> OperatorProfile:
        return OperatorProfile(
            id="default",
            display_name="操作者",
            timezone_id="Asia/Taipei",
        )

    async def get_for_user(self, user_id: str) -> OperatorProfile:
        return OperatorProfile(
            id=user_id,
            display_name="操作者",
            timezone_id="Asia/Taipei",
        )


class StubPersonaExtractionService:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def run_after_turn(self, **kwargs):
        self.calls.append(kwargs)


class StubOperatorPersonaService:
    async def get_current(self, character_id: str, operator_id: str):
        return OperatorPersona.empty(character_id, operator_id)

    def render_for_prompt(self, persona: OperatorPersona) -> list[str]:
        return []

    async def get_interaction_strength(
        self,
        character_id: str,
        operator_id: str,
    ) -> InteractionStrength:
        return InteractionStrength(
            character_id=character_id,
            operator_id=operator_id,
            first_message_at=None,
            total_user_messages=0,
            days_since_first_contact=0,
            messages_last_7_days=0,
            messages_last_30_days=0,
            longest_session_minutes=0,
            shared_arc_realized_count=0,
            shared_drama_count=0,
            familiarity_band=Familiarity.STRANGER,
            computed_at=datetime.now(timezone.utc),
        )


class StubRelationshipSeedRepository:
    async def get(
        self,
        character_id: str,
        operator_id: str,
    ) -> CharacterOperatorRelationshipSeed:
        return CharacterOperatorRelationshipSeed(
            character_id=character_id,
            operator_id=operator_id,
            relationship_label="老朋友",
        )


def build_chat_service(
    *,
    decider,
    schedule_service,
    pending_repo,
    persona_extraction_service=None,
    journal_repository=None,
    operator_profile_service=None,
    proactive_attempt_repository=None,
    operator_persona_service=None,
    relationship_seed_repository=None,
    release_enqueuer=None,
    player_persona_note_repository=None,
    character_repository=None,
    conversation_repository=None,
    memory_repository=None,
    encounter_intent_repository=None,
):
    """Wire a real ``ChatService`` around the stand-ins above.

    The three repository arguments exist for a caller that has to hold
    the same stores the chat path writes into — turn-undo, which is
    handed them separately and has to be looking at the very rows the
    turn wrote, not at copies.
    """
    character_repository = character_repository or InMemoryCharacterRepository()
    conversation_repository = (
        conversation_repository or InMemoryConversationRepository()
    )
    memory_repository = memory_repository or InMemoryMemoryRepository()
    registry = InMemoryChatModelRegistry(default_provider_id="fake")
    registry.register(FakeChatModel(provider_id="fake"))
    chat_service = ChatService(
        character_repository=character_repository,
        conversation_repository=conversation_repository,
        memory_repository=memory_repository,
        post_turn_processor=NullPostTurnProcessor(),
        prompt_context_builder=DefaultPromptContextBuilder(),
        model_registry=registry,
        state_engine=SimpleStateEngine(),
        schedule_service=schedule_service,
        busy_reply_decider=decider,
        pending_follow_up_repository=pending_repo,
        proactive_attempt_repository=proactive_attempt_repository,
        operator_profile_service=(
            operator_profile_service
            if operator_profile_service is not None
            else (
                StubOperatorProfileService()
                if persona_extraction_service is not None else None
            )
        ),
        persona_extraction_service=persona_extraction_service,
        operator_persona_service=operator_persona_service,
        relationship_seed_repository=relationship_seed_repository,
        journal_repository=journal_repository,
        player_persona_note_repository=player_persona_note_repository,
        character_encounter_intent_repository=encounter_intent_repository,
    )
    if release_enqueuer is not None:
        chat_service.set_pending_follow_up_release_enqueuer(release_enqueuer)
    character_service = CharacterService(
        character_repository,
        conversation_repository=conversation_repository,
        memory_repository=memory_repository,
        pending_follow_up_repository=pending_repo,
    )
    return chat_service, character_service, conversation_repository


class SpyReleaseEnqueuer:
    """Records the follow-up rows handed to the distributed release enqueuer."""

    def __init__(self) -> None:
        self.rows: list[Any] = []

    async def enqueue(self, row, *, now=None) -> bool:  # noqa: ANN001
        self.rows.append(row)
        return True
