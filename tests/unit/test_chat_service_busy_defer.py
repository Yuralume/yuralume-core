"""Integration tests for the busy-defer branch in :class:`ChatService`.

Exercises the happy paths the user-facing flow promises:

* When the decider says ``BRIEF_DEFER``, the user sees the brief reply
  inline and a ``PendingFollowUp`` row appears in the repository.
* A second user message in the same conversation is appended for audit,
  then cancels the row so the normal chat path replies immediately.
* When the decider says ``IMMEDIATE``, the chat path runs normally
  (no pending row written).
* When the current activity is below the perf floor, the decider is
  not even invoked.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from kokoro_link.application.dto.character import CreateCharacterRequest
from kokoro_link.application.dto.chat import SendChatMessageRequest
from kokoro_link.application.services.character_service import CharacterService
from kokoro_link.application.services.chat_service import ChatService
from kokoro_link.contracts.busy_reply_decider import (
    BusyDecision,
    BusyReplyMode,
)
from kokoro_link.domain.entities.pending_follow_up import (
    PendingFollowUpStatus,
)
from kokoro_link.domain.entities.character_operator_relationship_seed import (
    CharacterOperatorRelationshipSeed,
)
from kokoro_link.domain.entities.operator_persona import (
    InteractionStrength,
    OperatorPersona,
)
from kokoro_link.domain.entities.operator_profile import OperatorProfile
from kokoro_link.domain.entities.proactive_attempt import ProactiveAttempt
from kokoro_link.domain.value_objects.familiarity import Familiarity
from kokoro_link.domain.entities.schedule import ScheduleActivity
from kokoro_link.domain.value_objects.proactive_outcome import ProactiveOutcome
from kokoro_link.domain.value_objects.proactive_trigger import ProactiveTrigger
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
from kokoro_link.infrastructure.repositories.in_memory_pending_follow_ups import (
    InMemoryPendingFollowUpRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_proactive_attempts import (
    InMemoryProactiveAttemptRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_turn_journals import (
    InMemoryTurnJournalRepository,
)
from kokoro_link.infrastructure.state.simple import SimpleStateEngine


def _busy_activity(busy: float = 0.9) -> ScheduleActivity:
    now = datetime.now(timezone.utc)
    return ScheduleActivity.create(
        start_at=now - timedelta(minutes=30),
        end_at=now + timedelta(minutes=30),
        description="跟客戶開會",
        category="meeting",
        busy_score=busy,
    )


class _StubScheduleService:
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


class _ScriptedDecider:
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


class _StubOperatorProfileService:
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


class _StubPersonaExtractionService:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def run_after_turn(self, **kwargs):
        self.calls.append(kwargs)


class _StubOperatorPersonaService:
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


class _StubRelationshipSeedRepository:
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


def _build_chat_service(
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
):
    character_repository = InMemoryCharacterRepository()
    conversation_repository = InMemoryConversationRepository()
    memory_repository = InMemoryMemoryRepository()
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
                _StubOperatorProfileService()
                if persona_extraction_service is not None else None
            )
        ),
        persona_extraction_service=persona_extraction_service,
        operator_persona_service=operator_persona_service,
        relationship_seed_repository=relationship_seed_repository,
        journal_repository=journal_repository,
    )
    character_service = CharacterService(
        character_repository,
        conversation_repository=conversation_repository,
        memory_repository=memory_repository,
        pending_follow_up_repository=pending_repo,
    )
    return chat_service, character_service, conversation_repository


@pytest.mark.asyncio
async def test_brief_defer_persists_inline_reply_and_pending_row() -> None:
    activity = _busy_activity()
    decider = _ScriptedDecider([
        BusyDecision(
            mode=BusyReplyMode.BRIEF_DEFER,
            brief_reply="先回，等會議結束我再好好回你",
            defer_until=activity.end_at,
            defer_reason="會議中",
        ),
    ])
    pending_repo = InMemoryPendingFollowUpRepository()
    schedule = _StubScheduleService(current_activity=activity)
    chat, character_service, conversation_repository = _build_chat_service(
        decider=decider, schedule_service=schedule, pending_repo=pending_repo,
    )
    created = await character_service.create_character(
        CreateCharacterRequest(
            name="Airi", personality=["責任感重"], interests=[],
        ),
    )
    reply = await chat.send_message(
        SendChatMessageRequest(
            character_id=created.id,
            message="晚餐想吃什麼",
        ),
    )

    assert reply.assistant_message.content == "先回，等會議結束我再好好回你"
    # Conversation now has exactly user + assistant
    conv = await conversation_repository.get(reply.conversation_id)
    assert conv is not None
    assert len(conv.messages) == 2
    # Pending row created
    open_row = await pending_repo.find_open_for_conversation(reply.conversation_id)
    assert open_row is not None
    assert open_row.status == PendingFollowUpStatus.QUEUED
    assert open_row.messages[0].content == "晚餐想吃什麼"
    assert open_row.scheduled_for == activity.end_at


@pytest.mark.asyncio
async def test_busy_decider_receives_owner_timezone() -> None:
    activity = _busy_activity()
    decider = _ScriptedDecider([
        BusyDecision(
            mode=BusyReplyMode.BRIEF_DEFER,
            brief_reply="先回，晚點找你",
            defer_until=activity.end_at,
        ),
    ])
    pending_repo = InMemoryPendingFollowUpRepository()
    chat, character_service, _ = _build_chat_service(
        decider=decider,
        schedule_service=_StubScheduleService(current_activity=activity),
        pending_repo=pending_repo,
        operator_profile_service=_StubOperatorProfileService(),
    )
    created = await character_service.create_character(
        CreateCharacterRequest(name="Airi", personality=[], interests=[]),
    )

    await chat.send_message(
        SendChatMessageRequest(character_id=created.id, message="你在忙嗎"),
    )

    assert decider.calls
    local_tz = decider.calls[0]["local_tz"]
    assert local_tz is not None
    assert datetime(2026, 6, 14, 16, 30, tzinfo=timezone.utc).astimezone(
        local_tz,
    ) == datetime(2026, 6, 15, 0, 30, tzinfo=ZoneInfo("Asia/Taipei"))


@pytest.mark.asyncio
async def test_busy_decider_keeps_seed_relationship_above_low_interaction_band() -> None:
    activity = _busy_activity()
    decider = _ScriptedDecider([BusyDecision()])
    pending_repo = InMemoryPendingFollowUpRepository()
    chat, character_service, _ = _build_chat_service(
        decider=decider,
        schedule_service=_StubScheduleService(current_activity=activity),
        pending_repo=pending_repo,
        operator_profile_service=_StubOperatorProfileService(),
        operator_persona_service=_StubOperatorPersonaService(),
        relationship_seed_repository=_StubRelationshipSeedRepository(),
    )
    created = await character_service.create_character(
        CreateCharacterRequest(name="Airi", personality=[], interests=[]),
    )

    await chat.send_message(
        SendChatMessageRequest(character_id=created.id, message="等等有空嗎"),
    )

    relationship_lines = "\n".join(decider.calls[0]["relationship_context_lines"])
    interaction_lines = "\n".join(decider.calls[0]["interaction_context_lines"])
    assert "關係：老朋友" in relationship_lines
    assert "互動量還很少" in interaction_lines
    assert "起始關係設定是關係主述" in interaction_lines
    assert "破冰期" not in interaction_lines
    assert "全新" not in interaction_lines
    assert "剛認識" not in interaction_lines


@pytest.mark.asyncio
async def test_busy_decider_receives_recent_proactive_outreach() -> None:
    """The character's own just-sent proactive push is threaded into the
    busy decider so it can tell "the user is replying to outreach I just
    initiated" from "an unsolicited interruption mid-focus" — the bug
    where replying to a proactive ping got a busy brush-off."""
    activity = _busy_activity()
    # IMMEDIATE: we only assert what the decider was *given*, not its call.
    decider = _ScriptedDecider([BusyDecision()])
    pending_repo = InMemoryPendingFollowUpRepository()
    proactive_repo = InMemoryProactiveAttemptRepository()
    chat, character_service, _ = _build_chat_service(
        decider=decider,
        schedule_service=_StubScheduleService(current_activity=activity),
        pending_repo=pending_repo,
        proactive_attempt_repository=proactive_repo,
    )
    created = await character_service.create_character(
        CreateCharacterRequest(name="Airi", personality=[], interests=[]),
    )
    await proactive_repo.add(
        ProactiveAttempt.record(
            character_id=created.id,
            trigger=ProactiveTrigger.TICK,
            outcome=ProactiveOutcome.SENT,
            message="在開會但突然好想你",
            now=datetime.now(timezone.utc) - timedelta(minutes=4),
        ),
    )

    await chat.send_message(
        SendChatMessageRequest(
            character_id=created.id, message="我也想你，會議加油",
        ),
    )

    assert decider.calls
    attempts = decider.calls[0]["recent_proactive_attempts"]
    assert [a.message for a in attempts] == ["在開會但突然好想你"]


@pytest.mark.asyncio
async def test_brief_defer_records_journal_and_runs_persona_extraction() -> None:
    activity = _busy_activity()
    decider = _ScriptedDecider([
        BusyDecision(
            mode=BusyReplyMode.BRIEF_DEFER,
            brief_reply="先回，等等找你",
            defer_until=activity.end_at,
            defer_reason="會議中",
        ),
    ])
    pending_repo = InMemoryPendingFollowUpRepository()
    journal_repo = InMemoryTurnJournalRepository()
    persona = _StubPersonaExtractionService()
    chat, character_service, _conversation_repository = _build_chat_service(
        decider=decider,
        schedule_service=_StubScheduleService(current_activity=activity),
        pending_repo=pending_repo,
        persona_extraction_service=persona,
        journal_repository=journal_repo,
    )
    created = await character_service.create_character(
        CreateCharacterRequest(name="Airi", personality=[], interests=[]),
    )

    reply = await chat.send_message(
        SendChatMessageRequest(character_id=created.id, message="我是工程師"),
    )

    assert persona.calls
    assert persona.calls[0]["character_id"] == created.id
    assert persona.calls[0]["user_text"] == "我是工程師"
    assert await journal_repo.get_latest(reply.conversation_id) is not None


@pytest.mark.asyncio
async def test_brief_defer_respects_persona_disabled_flag() -> None:
    activity = _busy_activity()
    decider = _ScriptedDecider([
        BusyDecision(
            mode=BusyReplyMode.BRIEF_DEFER,
            brief_reply="先回",
            defer_until=activity.end_at,
            defer_reason="會議中",
        ),
    ])
    pending_repo = InMemoryPendingFollowUpRepository()
    persona = _StubPersonaExtractionService()
    chat, character_service, _ = _build_chat_service(
        decider=decider,
        schedule_service=_StubScheduleService(current_activity=activity),
        pending_repo=pending_repo,
        persona_extraction_service=persona,
    )
    created = await character_service.create_character(
        CreateCharacterRequest(name="Airi", personality=[], interests=[]),
    )

    await chat.send_message(
        SendChatMessageRequest(
            character_id=created.id,
            message="我是工程師",
            operator_persona_enabled=False,
        ),
    )

    assert persona.calls == []


@pytest.mark.asyncio
async def test_second_message_is_appended_before_pending_row_cancels() -> None:
    activity = _busy_activity()
    decider = _ScriptedDecider([
        BusyDecision(
            mode=BusyReplyMode.BRIEF_DEFER,
            brief_reply="先回，等等",
            defer_until=activity.end_at,
            defer_reason="會議中",
        ),
        BusyDecision(
            mode=BusyReplyMode.BRIEF_DEFER,
            brief_reply="嗯嗯收到",
            defer_until=activity.end_at,
            defer_reason="會議中",
        ),
    ])
    pending_repo = InMemoryPendingFollowUpRepository()
    schedule = _StubScheduleService(current_activity=activity)
    chat, character_service, _ = _build_chat_service(
        decider=decider, schedule_service=schedule, pending_repo=pending_repo,
    )
    created = await character_service.create_character(
        CreateCharacterRequest(name="Airi", personality=[], interests=[]),
    )
    first = await chat.send_message(
        SendChatMessageRequest(
            character_id=created.id, message="晚餐想吃什麼",
        ),
    )
    pending = await pending_repo.find_open_for_conversation(first.conversation_id)
    assert pending is not None
    await chat.send_message(
        SendChatMessageRequest(
            character_id=created.id,
            conversation_id=first.conversation_id,
            message="不然吃義大利麵好了",
        ),
    )

    open_rows = await pending_repo.list_open_for_character(created.id)
    assert open_rows == []
    merged = await pending_repo.get(pending.id)
    assert merged is not None
    assert merged.status == PendingFollowUpStatus.CANCELLED
    assert len(merged.messages) == 2
    assert merged.messages[0].content == "晚餐想吃什麼"
    assert merged.messages[1].content == "不然吃義大利麵好了"
    # scheduled_for preserved for audit even though normal reply takes over.
    assert merged.scheduled_for == activity.end_at


@pytest.mark.asyncio
async def test_existing_pending_follow_up_is_cancelled_and_next_turn_replies() -> None:
    activity = _busy_activity()
    decider = _ScriptedDecider([
        BusyDecision(
            mode=BusyReplyMode.BRIEF_DEFER,
            brief_reply="先回，等等",
            defer_until=activity.end_at,
            defer_reason="會議中",
        ),
        BusyDecision(mode=BusyReplyMode.IMMEDIATE),
    ])
    pending_repo = InMemoryPendingFollowUpRepository()
    schedule = _StubScheduleService(current_activity=activity)
    chat, character_service, conv_repo = _build_chat_service(
        decider=decider, schedule_service=schedule, pending_repo=pending_repo,
    )
    created = await character_service.create_character(
        CreateCharacterRequest(name="Airi", personality=[], interests=[]),
    )
    first = await chat.send_message(
        SendChatMessageRequest(
            character_id=created.id, message="晚餐想吃什麼",
        ),
    )
    pending = await pending_repo.find_open_for_conversation(first.conversation_id)
    assert pending is not None

    second = await chat.send_message(
        SendChatMessageRequest(
            character_id=created.id,
            conversation_id=first.conversation_id,
            message="不然吃義大利麵好了",
        ),
    )

    assert second.assistant_message is not None
    assert second.assistant_message.content
    assert len(decider.calls) == 1
    open_rows = await pending_repo.list_open_for_character(created.id)
    assert open_rows == []
    merged = await pending_repo.get(pending.id)
    assert merged is not None
    assert merged.status == PendingFollowUpStatus.CANCELLED
    assert [m.content for m in merged.messages] == [
        "晚餐想吃什麼",
        "不然吃義大利麵好了",
    ]
    conversation = await conv_repo.get(first.conversation_id)
    assert conversation is not None
    assert [m.role.value for m in conversation.messages] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    assert conversation.messages[-2].content == "不然吃義大利麵好了"


@pytest.mark.asyncio
async def test_existing_pending_follow_up_prevents_consecutive_defer() -> None:
    activity = _busy_activity()
    decider = _ScriptedDecider([
        BusyDecision(
            mode=BusyReplyMode.BRIEF_DEFER,
            brief_reply="先回，等等",
            defer_until=activity.end_at,
            defer_reason="會議中",
        ),
        BusyDecision(
            mode=BusyReplyMode.BRIEF_DEFER,
            brief_reply="又延後",
            defer_until=activity.end_at,
            defer_reason="會議中",
        ),
    ])
    pending_repo = InMemoryPendingFollowUpRepository()
    schedule = _StubScheduleService(current_activity=activity)
    chat, character_service, _ = _build_chat_service(
        decider=decider, schedule_service=schedule, pending_repo=pending_repo,
    )
    created = await character_service.create_character(
        CreateCharacterRequest(name="Airi", personality=[], interests=[]),
    )
    first = await chat.send_message(
        SendChatMessageRequest(character_id=created.id, message="晚餐想吃什麼"),
    )

    second = await chat.send_message(
        SendChatMessageRequest(
            character_id=created.id,
            conversation_id=first.conversation_id,
            message="你真的有看到嗎",
        ),
    )

    assert second.assistant_message is not None
    assert second.assistant_message.content != "又延後"
    assert len(decider.calls) == 1


@pytest.mark.asyncio
async def test_immediate_decision_runs_normal_path() -> None:
    activity = _busy_activity()
    decider = _ScriptedDecider([BusyDecision()])  # immediate
    pending_repo = InMemoryPendingFollowUpRepository()
    schedule = _StubScheduleService(current_activity=activity)
    chat, character_service, conv_repo = _build_chat_service(
        decider=decider, schedule_service=schedule, pending_repo=pending_repo,
    )
    created = await character_service.create_character(
        CreateCharacterRequest(name="Airi", personality=[], interests=[]),
    )
    reply = await chat.send_message(
        SendChatMessageRequest(
            character_id=created.id, message="你在嗎",
        ),
    )

    # No defer → fake provider's reply, not the brief
    assert reply.assistant_message.content != "先回"
    open_rows = await pending_repo.list_open_for_character(created.id)
    assert open_rows == []


@pytest.mark.asyncio
async def test_low_busy_activity_skips_decider_invocation() -> None:
    """Perf gate: skip the LLM call when patently idle."""
    activity = _busy_activity(busy=0.3)  # below the floor
    decider = _ScriptedDecider([
        # If the decider WERE invoked, this would defer — but we expect
        # the perf floor to skip the call entirely.
        BusyDecision(
            mode=BusyReplyMode.BRIEF_DEFER,
            brief_reply="should not fire",
            defer_until=activity.end_at,
        ),
    ])
    pending_repo = InMemoryPendingFollowUpRepository()
    schedule = _StubScheduleService(current_activity=activity)
    chat, character_service, _ = _build_chat_service(
        decider=decider, schedule_service=schedule, pending_repo=pending_repo,
    )
    created = await character_service.create_character(
        CreateCharacterRequest(name="Airi", personality=[], interests=[]),
    )
    await chat.send_message(
        SendChatMessageRequest(
            character_id=created.id, message="你在嗎",
        ),
    )

    assert decider.calls == []  # never invoked
    open_rows = await pending_repo.list_open_for_character(created.id)
    assert open_rows == []


@pytest.mark.asyncio
async def test_no_schedule_skips_decider() -> None:
    """No current activity → no busy context to defer on."""
    decider = _ScriptedDecider([
        BusyDecision(
            mode=BusyReplyMode.BRIEF_DEFER,
            brief_reply="should not fire",
            defer_until=datetime.now(timezone.utc) + timedelta(minutes=10),
        ),
    ])
    pending_repo = InMemoryPendingFollowUpRepository()
    schedule = _StubScheduleService(current_activity=None)
    chat, character_service, _ = _build_chat_service(
        decider=decider, schedule_service=schedule, pending_repo=pending_repo,
    )
    created = await character_service.create_character(
        CreateCharacterRequest(name="Airi", personality=[], interests=[]),
    )
    await chat.send_message(
        SendChatMessageRequest(
            character_id=created.id, message="你在嗎",
        ),
    )
    assert decider.calls == []
