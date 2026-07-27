"""Crash-recovery coverage for :class:`ExternalChatTurnService` (LH2, Core-C2).

Walks the DR-LH0-004 crash matrix with crash-injecting fake ChatServices that
leave the durable receipt in a realistic mid-flight state, then retries through
the REAL orchestrator and asserts convergence:

* user appended, then crash → retry does NOT rewrite the user and the LLM
  re-runs exactly once;
* GENERATED checkpoint, then crash → an expired-lease takeover finalises from
  the snapshot WITHOUT calling the LLM (generate count 0);
* COMMITTED, then crash → the retry replays the exact stored snapshot without
  the LLM and reaches COMPLETED;
* the busy-defer branch lands the receipt at COMPLETED via commit_deferred.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.contracts.busy_reply_decider import BusyDecision, BusyReplyMode
from kokoro_link.contracts.external_chat_execution import GeneratedTurnSnapshot
from kokoro_link.contracts.external_chat_turn import ExternalChatTurnState
from kokoro_link.domain.entities.conversation import Message, MessageRole
from kokoro_link.domain.entities.schedule import ScheduleActivity
from kokoro_link.infrastructure.repositories.in_memory_conversations import (
    InMemoryConversationRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_external_chat_turn import (
    InMemoryExternalChatTurnReceiptRepository,
)

from tests.unit.external_chat_turn_harness import (
    CountingFakeChatModel,
    build_harness,
    make_command,
)

pytestmark = pytest.mark.asyncio


def _only_receipt_id(receipt_repo: InMemoryExternalChatTurnReceiptRepository) -> str:
    ids = list(receipt_repo._records)
    assert len(ids) == 1, f"expected one receipt, got {ids}"
    return ids[0]


def _expire_lease(receipt_repo, turn_id: str) -> None:
    """Force the in-memory receipt's lease into the past (simulate a real
    process crash: the row is left mid-flight with no owner mark_failed)."""
    record = receipt_repo._records[turn_id]
    record.lease_until = datetime.now(timezone.utc) - timedelta(seconds=1)


class _CrashAfterUserAppend:
    """Persists the user turn (as ChatService would) then crashes."""

    def __init__(self, conversation_repo) -> None:
        self._conversations = conversation_repo

    async def send_message(self, request, *, current_user_id=None, external_turn=None):
        conversation = await self._conversations.get(request.conversation_id)
        user_message = Message(role=MessageRole.USER, content=request.message)
        await external_turn.persist_user_turn(conversation, user_message)
        raise RuntimeError("crash after user append")


class _CheckpointGeneratedThenStop:
    """Persists user + GENERATED checkpoint, then returns WITHOUT committing
    (a crash before the commit; the orchestrator leaves the receipt GENERATED
    because _complete_from_committed only marks completed a COMMITTED turn)."""

    def __init__(self, conversation_repo, assistant_text: str) -> None:
        self._conversations = conversation_repo
        self._assistant_text = assistant_text

    async def send_message(self, request, *, current_user_id=None, external_turn=None):
        conversation = await self._conversations.get(request.conversation_id)
        user_message = Message(role=MessageRole.USER, content=request.message)
        await external_turn.persist_user_turn(conversation, user_message)
        await external_turn.checkpoint_generated(
            GeneratedTurnSnapshot(
                turn_id=external_turn.stable_turn_id(),
                assistant_text=self._assistant_text,
                attachments=(),
                assistant_kind="chat",
                content_mode="normal",
            ),
        )
        # No commit — simulate a crash right after the durable GENERATED.


class _CommitThenCrash:
    """Persists user + GENERATED + COMMITTED (assistant durably appended and
    the response snapshot stored) then crashes before COMPLETED."""

    def __init__(self, conversation_repo, assistant_text: str) -> None:
        self._conversations = conversation_repo
        self._assistant_text = assistant_text

    async def send_message(self, request, *, current_user_id=None, external_turn=None):
        conversation = await self._conversations.get(request.conversation_id)
        user_message = Message(role=MessageRole.USER, content=request.message)
        await external_turn.persist_user_turn(conversation, user_message)
        assistant_message = Message(
            role=MessageRole.ASSISTANT, content=self._assistant_text,
        )
        await external_turn.checkpoint_generated(
            GeneratedTurnSnapshot(
                turn_id=external_turn.stable_turn_id(),
                assistant_text=self._assistant_text,
                attachments=(),
                assistant_kind="chat",
                content_mode="normal",
            ),
        )
        await external_turn.commit_assistant_turn(conversation, assistant_message)
        raise RuntimeError("crash after committed")


async def test_crash_after_user_append_reruns_llm_once_no_duplicate_user() -> None:
    receipt_repo = InMemoryExternalChatTurnReceiptRepository()
    conversation_repo = InMemoryConversationRepository()

    # First attempt: persist user, then crash → FAILED_RETRYABLE, user durable.
    crash = await build_harness(
        receipt_repo=receipt_repo, conversation_repo=conversation_repo,
        chat_service_override=_CrashAfterUserAppend(conversation_repo),
    )
    command = make_command()
    first = await crash.turn_service.execute(command)
    assert first.status_code == 503
    turn_id = _only_receipt_id(receipt_repo)
    receipt = await receipt_repo.get(turn_id)
    assert receipt.state is ExternalChatTurnState.FAILED_RETRYABLE
    assert receipt.user_message_row_id is not None
    conversation_id = receipt.conversation_id
    assert conversation_id is not None

    # Retry with a REAL ChatService (counting LLM): FAILED_RETRYABLE takeover
    # reuses the same turn_id + user row; the LLM re-runs exactly once.
    model = CountingFakeChatModel()
    retry = await build_harness(
        receipt_repo=receipt_repo, conversation_repo=conversation_repo,
        model=model,
    )
    result = await retry.turn_service.execute(command)

    assert result.status_code == 200
    assert model.generate_calls == 1
    receipt = await receipt_repo.get(turn_id)
    assert receipt.state is ExternalChatTurnState.COMPLETED
    conversation = await conversation_repo.get(conversation_id)
    # Exactly one user turn (never rewritten) + one assistant turn.
    roles = [m.role for m in conversation.messages]
    assert roles == [MessageRole.USER, MessageRole.ASSISTANT]


async def test_crash_after_generated_finalizes_without_llm() -> None:
    receipt_repo = InMemoryExternalChatTurnReceiptRepository()
    conversation_repo = InMemoryConversationRepository()

    crash = await build_harness(
        receipt_repo=receipt_repo, conversation_repo=conversation_repo,
        chat_service_override=_CheckpointGeneratedThenStop(
            conversation_repo, "生成好的回覆",
        ),
    )
    command = make_command()
    first = await crash.turn_service.execute(command)
    assert first.status_code == 503
    turn_id = _only_receipt_id(receipt_repo)
    receipt = await receipt_repo.get(turn_id)
    assert receipt.state is ExternalChatTurnState.GENERATED
    assert receipt.assistant_message_row_id is None

    # Real crash: expire the live lease so the retry is a GeneratedRecovery.
    _expire_lease(receipt_repo, turn_id)

    model = CountingFakeChatModel()
    retry = await build_harness(
        receipt_repo=receipt_repo, conversation_repo=conversation_repo,
        model=model,
    )
    result = await retry.turn_service.execute(command)

    assert result.status_code == 200
    # The LLM is NEVER re-run on a GENERATED takeover.
    assert model.generate_calls == 0
    receipt = await receipt_repo.get(turn_id)
    assert receipt.state is ExternalChatTurnState.COMPLETED
    conversation = await conversation_repo.get(receipt.conversation_id)
    assert [m.role for m in conversation.messages] == [
        MessageRole.USER, MessageRole.ASSISTANT,
    ]
    assert conversation.messages[1].content == "生成好的回覆"


async def test_crash_after_committed_replays_snapshot_without_llm() -> None:
    receipt_repo = InMemoryExternalChatTurnReceiptRepository()
    conversation_repo = InMemoryConversationRepository()

    crash = await build_harness(
        receipt_repo=receipt_repo, conversation_repo=conversation_repo,
        chat_service_override=_CommitThenCrash(conversation_repo, "已提交的回覆"),
    )
    command = make_command()
    first = await crash.turn_service.execute(command)
    assert first.status_code == 503
    turn_id = _only_receipt_id(receipt_repo)
    receipt = await receipt_repo.get(turn_id)
    # mark_failed can't touch a COMMITTED row → it stays COMMITTED with snapshot.
    assert receipt.state is ExternalChatTurnState.COMMITTED
    committed_snapshot = receipt.response_snapshot_json
    assert committed_snapshot is not None

    model = CountingFakeChatModel()
    retry = await build_harness(
        receipt_repo=receipt_repo, conversation_repo=conversation_repo,
        model=model,
    )
    result = await retry.turn_service.execute(command)

    assert result.status_code == 200
    # CommittedRecovery replays the exact stored snapshot; no LLM.
    assert result.body_json == committed_snapshot
    assert model.generate_calls == 0
    receipt = await receipt_repo.get(turn_id)
    assert receipt.state is ExternalChatTurnState.COMPLETED


def _busy_activity(busy: float = 0.9) -> ScheduleActivity:
    now = datetime.now(timezone.utc)
    return ScheduleActivity.create(
        start_at=now - timedelta(minutes=30),
        end_at=now + timedelta(minutes=30),
        description="跟客戶開會",
        category="meeting",
        busy_score=busy,
    )


class _ScriptedDecider:
    def __init__(self, decision: BusyDecision) -> None:
        self._decision = decision

    async def decide(self, **kwargs):
        return self._decision


async def test_busy_defer_lands_receipt_committed_then_completed() -> None:
    decider = _ScriptedDecider(
        BusyDecision(
            mode=BusyReplyMode.BRIEF_DEFER,
            brief_reply="先回，等會議結束再好好回你",
            defer_until=datetime.now(timezone.utc) + timedelta(minutes=30),
            defer_reason="會議中",
        ),
    )
    harness = await build_harness(
        decider=decider, current_activity=_busy_activity(),
    )
    command = make_command(message="晚餐想吃什麼")

    result = await harness.turn_service.execute(command)

    assert result.status_code == 200
    body = __import__("json").loads(result.body_json)
    assert body["segments"][0]["text"] == "先回，等會議結束再好好回你"
    receipt = await harness.receipt_repo.get(body["turn_id"])
    assert receipt.state is ExternalChatTurnState.COMPLETED
    conversation = await harness.conversation_repo.get(body["conversation_id"])
    # Busy-defer finalised user + brief through the deferred write-exit.
    assert [m.role for m in conversation.messages] == [
        MessageRole.USER, MessageRole.ASSISTANT,
    ]
