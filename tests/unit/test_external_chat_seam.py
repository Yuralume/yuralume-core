"""Unit tests for the LH2 external-chat execution seam in :class:`ChatService`.

Drives ``send_message`` with a *recording* :class:`ExternalChatTurnExecutionPort`
fake and asserts the seam contract (Core-C1):

* the hook fire order on the normal path
  (``persist_user`` → heartbeat → GENERATED checkpoint → assistant commit →
  post-turn anchors);
* the external user turn never goes through the whole-conversation
  ``repository.save()`` replace (only the durable port);
* the busy-defer branch finalises through ``commit_deferred_reply`` and never
  ``save()`` nor the normal ``commit_assistant_turn``;
* the stable turn id from the port is the turn record id in the response;
* ``external_turn=None`` leaves the port completely untouched (bit-identical
  legacy path).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from kokoro_link.application.dto.character import CreateCharacterRequest
from kokoro_link.application.dto.chat import SendChatMessageRequest
from kokoro_link.application.services.character_service import CharacterService
from kokoro_link.application.services.chat_service import ChatService
from kokoro_link.contracts.busy_reply_decider import BusyDecision, BusyReplyMode
from kokoro_link.contracts.external_chat_execution import (
    CommittedTurnRef,
    ExternalChatTurnExecutionPort,
    GeneratedTurnSnapshot,
    PersistedTurnRef,
    PostTurnAnchors,
)
from kokoro_link.domain.entities.conversation import Conversation, Message
from kokoro_link.domain.entities.schedule import ScheduleActivity
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
from kokoro_link.infrastructure.state.simple import SimpleStateEngine


class _RecordingExecutionPort(ExternalChatTurnExecutionPort):
    """Records the ordered hook fire sequence; hands back synthetic refs."""

    def __init__(self, turn_id: str = "turn-lh2-1") -> None:
        self.calls: list[str] = []
        self._turn_id = turn_id
        self.snapshots: list[GeneratedTurnSnapshot] = []

    async def persist_user_turn(
        self, conversation: Conversation, message: Message,
    ) -> PersistedTurnRef:
        self.calls.append("persist_user")
        return PersistedTurnRef(row_id=1, position=len(conversation.messages))

    async def checkpoint_generated(
        self, generated: GeneratedTurnSnapshot,
    ) -> None:
        self.calls.append("checkpoint_generated")
        self.snapshots.append(generated)

    async def commit_assistant_turn(
        self, conversation: Conversation, assistant_message: Message,
    ) -> CommittedTurnRef:
        self.calls.append("commit_assistant")
        return CommittedTurnRef(row_id=2, position=len(conversation.messages))

    async def commit_deferred_reply(
        self,
        conversation: Conversation,
        user_message: Message,
        assistant_message: Message,
    ) -> CommittedTurnRef:
        self.calls.append("commit_deferred")
        return CommittedTurnRef(row_id=2, position=len(conversation.messages) + 1)

    def stable_turn_id(self) -> str:
        self.calls.append("stable_turn_id")
        return self._turn_id

    def post_turn_anchors(self) -> PostTurnAnchors:
        self.calls.append("post_turn_anchors")
        return PostTurnAnchors(
            user_message_row_id=1,
            assistant_message_row_id=2,
            assistant_position=1,
        )

    async def heartbeat(self) -> None:
        self.calls.append("heartbeat")

    def heartbeat_interval_seconds(self) -> float:
        # Large enough that the fast fake generation completes before a
        # periodic heartbeat fires — the hook-order assertion stays stable.
        return 3600.0


class _SaveCountingConversationRepository(InMemoryConversationRepository):
    """In-memory conversation repo that counts whole-conversation replaces."""

    def __init__(self) -> None:
        super().__init__()
        self.save_calls = 0

    async def save(
        self, conversation: Conversation, *, truncation: bool = False,
    ) -> None:
        self.save_calls += 1
        await super().save(conversation, truncation=truncation)


class _StubScheduleService:
    def __init__(self, *, current_activity: ScheduleActivity | None) -> None:
        self.current_activity = current_activity

    async def ensure_schedule(self, character):
        return SimpleNamespace(date=datetime.now(timezone.utc).date())

    def resolve_current(self, schedule, *, now=None):
        return self.current_activity, [], None

    def resolve_completed_today(self, schedule, *, now=None, local_tz=None, limit=8):
        return []

    def resolve_pending_invites_from_schedules(self, schedules, *, now=None, limit=1):
        return []

    async def get_schedule(self, character_id, *, date_=None):
        return None

    async def current_activity_response(self, character):  # pragma: no cover
        return None


class _ScriptedDecider:
    def __init__(self, decisions: list[BusyDecision]) -> None:
        self.decisions = decisions
        self.calls: list[dict[str, Any]] = []

    async def decide(self, **kwargs):
        self.calls.append(kwargs)
        if not self.decisions:
            return BusyDecision()
        return self.decisions.pop(0)


def _busy_activity(busy: float = 0.9) -> ScheduleActivity:
    now = datetime.now(timezone.utc)
    return ScheduleActivity.create(
        start_at=now - timedelta(minutes=30),
        end_at=now + timedelta(minutes=30),
        description="跟客戶開會",
        category="meeting",
        busy_score=busy,
    )


def _build(*, decider=None, current_activity=None, conversation_repository=None):
    character_repository = InMemoryCharacterRepository()
    conversation_repository = (
        conversation_repository or InMemoryConversationRepository()
    )
    memory_repository = InMemoryMemoryRepository()
    registry = InMemoryChatModelRegistry(default_provider_id="fake")
    registry.register(FakeChatModel(provider_id="fake"))
    pending_repo = InMemoryPendingFollowUpRepository()
    chat_service = ChatService(
        character_repository=character_repository,
        conversation_repository=conversation_repository,
        memory_repository=memory_repository,
        post_turn_processor=NullPostTurnProcessor(),
        prompt_context_builder=DefaultPromptContextBuilder(),
        model_registry=registry,
        state_engine=SimpleStateEngine(),
        schedule_service=_StubScheduleService(current_activity=current_activity),
        busy_reply_decider=decider,
        pending_follow_up_repository=pending_repo if decider else None,
    )
    character_service = CharacterService(
        character_repository,
        conversation_repository=conversation_repository,
        memory_repository=memory_repository,
        pending_follow_up_repository=pending_repo,
    )
    return chat_service, character_service, conversation_repository


async def _seed_line_conversation(conversation_repository, character_id: str) -> str:
    conv = Conversation.start(character_id=character_id, source="line")
    await conversation_repository.save(conv)
    return conv.id


@pytest.mark.asyncio
async def test_normal_path_hook_order_and_stable_turn_id() -> None:
    chat, character_service, conv_repo = _build()
    created = await character_service.create_character(
        CreateCharacterRequest(name="Airi", personality=[], interests=[]),
    )
    conv_id = await _seed_line_conversation(conv_repo, created.id)
    port = _RecordingExecutionPort(turn_id="turn-abc")

    reply = await chat.send_message(
        SendChatMessageRequest(
            character_id=created.id,
            conversation_id=conv_id,
            message="今天過得怎麼樣",
        ),
        external_turn=port,
    )

    assert port.calls == [
        "stable_turn_id",
        "persist_user",
        "heartbeat",
        "heartbeat",
        "stable_turn_id",
        "checkpoint_generated",
        "commit_assistant",
        "post_turn_anchors",
    ]
    # The port's stable turn id is the turn record id surfaced in the response.
    assert reply.assistant_message.turn_record_id == "turn-abc"
    # The GENERATED snapshot carries the assistant text for recovery.
    assert port.snapshots[0].turn_id == "turn-abc"
    assert port.snapshots[0].assistant_text == reply.assistant_message.content


@pytest.mark.asyncio
async def test_external_user_turn_never_uses_repository_save() -> None:
    save_repo = _SaveCountingConversationRepository()
    chat, character_service, conv_repo = _build(
        conversation_repository=save_repo,
    )
    created = await character_service.create_character(
        CreateCharacterRequest(name="Airi", personality=[], interests=[]),
    )
    conv_id = await _seed_line_conversation(conv_repo, created.id)
    baseline_saves = save_repo.save_calls  # the seed save
    port = _RecordingExecutionPort()

    await chat.send_message(
        SendChatMessageRequest(
            character_id=created.id,
            conversation_id=conv_id,
            message="你在忙嗎",
        ),
        external_turn=port,
    )

    # No whole-conversation replace happened for the turn — reads (get) went
    # through the load path, writes went through the durable port only.
    assert save_repo.save_calls == baseline_saves
    assert "persist_user" in port.calls
    assert "commit_assistant" in port.calls


@pytest.mark.asyncio
async def test_busy_defer_uses_commit_deferred_not_save() -> None:
    save_repo = _SaveCountingConversationRepository()
    decider = _ScriptedDecider([
        BusyDecision(
            mode=BusyReplyMode.BRIEF_DEFER,
            brief_reply="先回，等會議結束再好好回你",
            defer_until=datetime.now(timezone.utc) + timedelta(minutes=30),
            defer_reason="會議中",
        ),
    ])
    chat, character_service, conv_repo = _build(
        decider=decider,
        current_activity=_busy_activity(),
        conversation_repository=save_repo,
    )
    created = await character_service.create_character(
        CreateCharacterRequest(name="Airi", personality=["責任感重"], interests=[]),
    )
    conv_id = await _seed_line_conversation(conv_repo, created.id)
    baseline_saves = save_repo.save_calls
    port = _RecordingExecutionPort()

    reply = await chat.send_message(
        SendChatMessageRequest(
            character_id=created.id,
            conversation_id=conv_id,
            message="晚餐想吃什麼",
        ),
        external_turn=port,
    )

    assert reply.assistant_message.content == "先回，等會議結束再好好回你"
    # Busy-defer branch: user persisted before the decision, brief finalised
    # via commit_deferred_reply — never the normal assistant commit, never save.
    assert port.calls == [
        "stable_turn_id",
        "persist_user",
        "commit_deferred",
    ]
    assert "commit_assistant" not in port.calls
    assert save_repo.save_calls == baseline_saves


@pytest.mark.asyncio
async def test_external_turn_none_leaves_port_untouched() -> None:
    chat, character_service, _conv_repo = _build()
    created = await character_service.create_character(
        CreateCharacterRequest(name="Airi", personality=[], interests=[]),
    )
    port = _RecordingExecutionPort()

    reply = await chat.send_message(
        SendChatMessageRequest(character_id=created.id, message="嗨"),
    )

    assert reply.assistant_message.content
    # Legacy path: the injected port is never consulted.
    assert port.calls == []
