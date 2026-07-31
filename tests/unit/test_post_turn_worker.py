"""Unit tests for event-driven post-turn processing as a one-shot job (§5 / §11).

Covers the two collaborators (enqueuer / handler) + the runner routing against the
full-fidelity in-memory queue + lease, plus the id-only rebuild on a real
``ChatService`` (in-memory repos + a spy post-turn processor) so the payload red line,
the append-only index anchor and the §3.3 re-verify are exercised for real.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.application.dto.character import CreateCharacterRequest
from kokoro_link.application.services.character_service import CharacterService
from kokoro_link.application.services.chat_service import ChatService
from kokoro_link.application.services.execution_mode_runner import (
    ExecutionModeRunner,
)
from kokoro_link.application.services.post_turn_runner import (
    PostTurnEnqueueOutcome,
    PostTurnEnqueuer,
    PostTurnHandler,
    PostTurnHandlerResult,
)
from kokoro_link.contracts.background_jobs import (
    COORDINATOR_LEASE_NAME,
    ClaimedJob,
    JobOutcome,
    JobStatus,
)
from kokoro_link.contracts.due_jobs import POST_TURN_KIND
from kokoro_link.contracts.post_turn import PostTurnResult
from kokoro_link.domain.entities.conversation import (
    Conversation, Message, MessageRole,
)
from kokoro_link.infrastructure.llm.fake import FakeChatModel
from kokoro_link.infrastructure.llm.registry import InMemoryChatModelRegistry
from kokoro_link.infrastructure.memory.in_memory import InMemoryMemoryRepository
from kokoro_link.infrastructure.post_turn.null_processor import NullPostTurnProcessor
from kokoro_link.infrastructure.prompt.default import DefaultPromptContextBuilder
from kokoro_link.infrastructure.repositories.in_memory_background_jobs import (
    InMemoryBackgroundCoordinatorLease,
    InMemoryBackgroundJobQueue,
)
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_conversations import (
    InMemoryConversationRepository,
)
from kokoro_link.infrastructure.state.simple import SimpleStateEngine

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #

async def _leased_queue() -> tuple[
    InMemoryBackgroundJobQueue, InMemoryBackgroundCoordinatorLease, int,
]:
    lease = InMemoryBackgroundCoordinatorLease()
    queue = InMemoryBackgroundJobQueue(lease=lease)
    epoch = await lease.acquire(
        COORDINATOR_LEASE_NAME, "coord", ttl_seconds=3600, now=NOW,
    )
    assert epoch is not None
    return queue, lease, epoch


async def _active_post_turn_jobs(queue: InMemoryBackgroundJobQueue) -> int:
    stats = await queue.stats(now=NOW)
    return stats.kind_counts.get(POST_TURN_KIND, 0)


def _enqueue_kwargs(**overrides) -> dict:
    base = dict(
        turn_record_id="turn-1",
        conversation_id="conv-1",
        character_id="char-1",
        assistant_index=3,
        persona_enabled=True,
        content_mode="normal",
    )
    base.update(overrides)
    return base


def _claimed_post_turn(**payload_overrides) -> ClaimedJob:
    payload = {
        "turn_record_id": "turn-1",
        "conversation_id": "conv-1",
        "character_id": "char-1",
        "assistant_index": 3,
        "persona_enabled": True,
        "content_mode": "normal",
    }
    payload.update(payload_overrides)
    return ClaimedJob(
        id="job-1", kind=POST_TURN_KIND, attempt_count=1, max_attempts=5,
        fencing_epoch=1, idempotency_key="post_turn:turn-1", priority=3,
        due_at=NOW, payload_version=1, payload=payload, lease_owner="w1",
        lease_until=NOW + timedelta(seconds=120), character_id="char-1",
    )


# --------------------------------------------------------------------------- #
# Enqueuer
# --------------------------------------------------------------------------- #

async def test_enqueue_creates_priority_three_post_turn_job() -> None:
    queue, lease, _ = await _leased_queue()
    enqueuer = PostTurnEnqueuer(queue=queue, coordinator_lease=lease)

    assert await enqueuer.enqueue(**_enqueue_kwargs(), now=NOW) is (
        PostTurnEnqueueOutcome.INSERTED
    )

    claimed = await queue.claim("w1", now=NOW, limit=10, lease_seconds=120)
    job = next(j for j in claimed if j.kind == POST_TURN_KIND)
    assert job.priority == 3
    assert job.due_at == NOW
    assert job.character_id == "char-1"
    assert job.payload == {
        "turn_record_id": "turn-1",
        "conversation_id": "conv-1",
        "character_id": "char-1",
        "assistant_index": 3,
        "persona_enabled": True,
        "content_mode": "normal",
    }


async def test_enqueue_payload_carries_no_chat_content() -> None:
    # Payload red line (§3.1 / §11): ids / flags / an enum ONLY — never message text.
    queue, lease, _ = await _leased_queue()
    enqueuer = PostTurnEnqueuer(queue=queue, coordinator_lease=lease)
    await enqueuer.enqueue(**_enqueue_kwargs(), now=NOW)

    [job] = await queue.claim("w1", now=NOW, limit=10, lease_seconds=120)
    keys = set(job.payload.keys())
    assert keys == {
        "turn_record_id", "conversation_id", "character_id",
        "assistant_index", "persona_enabled", "content_mode",
    }
    # No free-text content field leaked in.
    for forbidden in ("user_text", "assistant_text", "prior_messages", "prompt"):
        assert forbidden not in keys
    # Every value is an id / flag / enum — no message-length prose.
    assert all(
        isinstance(v, (str, int, bool)) for v in job.payload.values()
    )


async def test_enqueue_noop_without_leader() -> None:
    lease = InMemoryBackgroundCoordinatorLease()  # never acquired → embedded
    queue = InMemoryBackgroundJobQueue(lease=lease)
    enqueuer = PostTurnEnqueuer(queue=queue, coordinator_lease=lease)

    assert await enqueuer.enqueue(**_enqueue_kwargs(), now=NOW) is (
        PostTurnEnqueueOutcome.NO_LEADER
    )
    assert await _active_post_turn_jobs(queue) == 0


async def test_enqueue_noop_when_lease_expired() -> None:
    lease = InMemoryBackgroundCoordinatorLease()
    queue = InMemoryBackgroundJobQueue(lease=lease)
    await lease.acquire(COORDINATOR_LEASE_NAME, "coord", ttl_seconds=30, now=NOW)
    enqueuer = PostTurnEnqueuer(queue=queue, coordinator_lease=lease)

    later = NOW + timedelta(minutes=5)  # past the 30s lease → would fence out
    assert await enqueuer.enqueue(**_enqueue_kwargs(), now=later) is (
        PostTurnEnqueueOutcome.NO_LEADER
    )
    assert await _active_post_turn_jobs(queue) == 0


async def test_enqueue_is_idempotent_per_turn() -> None:
    # A second enqueue of the same turn (a live-leader idempotency collision) is
    # ALREADY_ACTIVE — a worker owns it — NOT the old ambiguous ``False`` that would
    # have driven a double in-process run (finding #11).
    queue, lease, _ = await _leased_queue()
    enqueuer = PostTurnEnqueuer(queue=queue, coordinator_lease=lease)

    assert await enqueuer.enqueue(**_enqueue_kwargs(), now=NOW) is (
        PostTurnEnqueueOutcome.INSERTED
    )
    second = await enqueuer.enqueue(**_enqueue_kwargs(), now=NOW)
    assert second is PostTurnEnqueueOutcome.ALREADY_ACTIVE
    assert second.should_fallback is False  # must NOT double-run in-process
    assert await _active_post_turn_jobs(queue) == 1


class _CommitThenRaiseOnce:
    """Wraps a queue so the first enqueue COMMITS then raises (response-loss): the
    row exists but the caller sees an exception."""

    def __init__(self, inner) -> None:  # noqa: ANN001
        self._inner = inner
        self._raised = False

    async def enqueue(self, spec, *, now):  # noqa: ANN001
        result = await self._inner.enqueue(spec, now=now)
        if not self._raised:
            self._raised = True
            raise RuntimeError("connection dropped after commit")
        return result


class _RaiseOnceNoCommit:
    """Wraps a queue so the first enqueue raises BEFORE committing; the retry then
    inserts normally (a definite pre-commit failure)."""

    def __init__(self, inner) -> None:  # noqa: ANN001
        self._inner = inner
        self._raised = False

    async def enqueue(self, spec, *, now):  # noqa: ANN001
        if not self._raised:
            self._raised = True
            raise RuntimeError("transient before send")
        return await self._inner.enqueue(spec, now=now)


class _AlwaysRaise:
    async def enqueue(self, spec, *, now):  # noqa: ANN001
        raise RuntimeError("queue down")


async def test_enqueue_ambiguous_after_commit_converges_to_already_active() -> None:
    # The commit succeeded but the response was lost. The bounded retry sees the
    # active row and classifies ALREADY_ACTIVE → the caller must NOT double-run.
    queue, lease, _ = await _leased_queue()
    enqueuer = PostTurnEnqueuer(
        queue=_CommitThenRaiseOnce(queue), coordinator_lease=lease,
    )

    outcome = await enqueuer.enqueue(**_enqueue_kwargs(), now=NOW)

    assert outcome is PostTurnEnqueueOutcome.ALREADY_ACTIVE
    assert outcome.should_fallback is False
    assert await _active_post_turn_jobs(queue) == 1  # exactly one row


async def test_enqueue_ambiguous_before_commit_retry_inserts() -> None:
    # A pre-commit failure: the retry inserts the (still-absent) row → INSERTED.
    queue, lease, _ = await _leased_queue()
    enqueuer = PostTurnEnqueuer(
        queue=_RaiseOnceNoCommit(queue), coordinator_lease=lease,
    )

    outcome = await enqueuer.enqueue(**_enqueue_kwargs(), now=NOW)

    assert outcome is PostTurnEnqueueOutcome.INSERTED
    assert await _active_post_turn_jobs(queue) == 1


async def test_enqueue_persistently_ambiguous_does_not_fallback() -> None:
    # The queue keeps raising: the outcome stays AMBIGUOUS after the bounded retry.
    # A row MAY exist, so we drop this best-effort post-turn rather than risk a
    # double non-idempotent run — should_fallback is False.
    _queue, lease, _ = await _leased_queue()
    enqueuer = PostTurnEnqueuer(queue=_AlwaysRaise(), coordinator_lease=lease)

    outcome = await enqueuer.enqueue(**_enqueue_kwargs(), now=NOW)

    assert outcome is PostTurnEnqueueOutcome.AMBIGUOUS
    assert outcome.should_fallback is False


async def test_no_leader_is_the_only_fallback_outcome() -> None:
    assert PostTurnEnqueueOutcome.NO_LEADER.should_fallback is True
    for other in (
        PostTurnEnqueueOutcome.INSERTED,
        PostTurnEnqueueOutcome.ALREADY_ACTIVE,
        PostTurnEnqueueOutcome.AMBIGUOUS,
    ):
        assert other.should_fallback is False


# --------------------------------------------------------------------------- #
# Handler
# --------------------------------------------------------------------------- #

@dataclass
class _SpyRunner:
    result: dict = field(default_factory=dict)
    calls: list[dict] = field(default_factory=list)

    async def __call__(self, **kwargs):  # noqa: ANN003
        self.calls.append(kwargs)
        return self.result


async def test_handler_delegates_to_runner() -> None:
    runner = _SpyRunner(result={"memory_ids": []})
    handler = PostTurnHandler(runner=runner)

    result = await handler.handle(_claimed_post_turn(), now=NOW)

    assert result == PostTurnHandlerResult(executed=True, reason="executed")
    assert runner.calls[0]["turn_record_id"] == "turn-1"
    assert runner.calls[0]["conversation_id"] == "conv-1"
    assert runner.calls[0]["assistant_index"] == 3
    assert runner.calls[0]["persona_enabled"] is True
    assert runner.calls[0]["content_mode"] == "normal"


async def test_handler_maps_skipped_reason() -> None:
    runner = _SpyRunner(result={"post_turn_skipped": "character_stopped"})
    handler = PostTurnHandler(runner=runner)

    result = await handler.handle(_claimed_post_turn(), now=NOW)

    assert result.executed is False
    assert result.reason == "character_stopped"


async def test_handler_rejects_bad_payload() -> None:
    runner = _SpyRunner()
    handler = PostTurnHandler(runner=runner)

    result = await handler.handle(_claimed_post_turn(assistant_index="x"), now=NOW)

    assert result.executed is False and result.reason == "bad_payload"
    assert runner.calls == []  # never delegated


# --------------------------------------------------------------------------- #
# Runner routing
# --------------------------------------------------------------------------- #

@dataclass
class _SpyHandler:
    result: PostTurnHandlerResult
    jobs: list[str] = field(default_factory=list)

    async def handle(self, job, *, now=None):  # noqa: ANN001
        self.jobs.append(job.id)
        return self.result


def _runner(queue, post_turn_handler) -> ExecutionModeRunner:
    return ExecutionModeRunner(
        queue=queue,
        character_repository=None,
        character_tick_executor=None,
        social_tick_executor=None,
        gate_service=None,
        cursor=None,
        bucket_seconds=300,
        heartbeat_interval_seconds=3600,
        post_turn_handler=post_turn_handler,
    )


async def test_runner_routes_post_turn_even_when_would_run_false() -> None:
    queue = InMemoryBackgroundJobQueue()
    spy = _SpyHandler(PostTurnHandlerResult(executed=True, reason="executed"))
    runner = _runner(queue, spy)

    disposition = await runner.execute(
        _claimed_post_turn(),
        would_run=False,  # per-character gate says skip — post-turn re-verifies itself
        skip_reason="frozen",
        now=NOW, worker_id="w1", lease_seconds=120,
    )

    assert disposition.action == "complete"
    assert disposition.outcome["executed"] is True
    assert disposition.outcome["kind"] == POST_TURN_KIND
    assert spy.jobs == ["job-1"]


async def test_runner_post_turn_handler_unwired_fails_retry() -> None:
    queue = InMemoryBackgroundJobQueue()
    runner = _runner(queue, None)

    disposition = await runner.execute(
        _claimed_post_turn(),
        would_run=True, skip_reason=None,
        now=NOW, worker_id="w1", lease_seconds=120,
    )

    assert disposition.action == "fail_retry"
    assert isinstance(disposition.error, RuntimeError)
    assert "handler_unwired" in str(disposition.error)


async def test_runner_enqueue_claim_execute_end_to_end() -> None:
    queue, lease, _ = await _leased_queue()
    enqueuer = PostTurnEnqueuer(queue=queue, coordinator_lease=lease)
    await enqueuer.enqueue(**_enqueue_kwargs(), now=NOW)
    spy = _SpyHandler(PostTurnHandlerResult(executed=True, reason="executed"))
    runner = _runner(queue, spy)

    [claimed] = await queue.claim("w1", now=NOW, limit=10, lease_seconds=120)
    disposition = await runner.execute(
        claimed, would_run=True, skip_reason=None,
        now=NOW, worker_id="w1", lease_seconds=120,
    )
    assert disposition.action == "complete"
    assert await queue.complete(claimed.id, "w1", outcome=JobOutcome(), now=NOW)

    # One-shot: nothing chained behind it.
    stats = await queue.stats(now=NOW)
    assert stats.status_counts.get(JobStatus.QUEUED.value, 0) == 0
    assert stats.status_counts.get(JobStatus.CLAIMED.value, 0) == 0


# --------------------------------------------------------------------------- #
# id-only rebuild on a real ChatService (in-memory repos + spy processor)
# --------------------------------------------------------------------------- #

@dataclass
class _SpyPostTurnProcessor:
    seen: dict = field(default_factory=dict)

    async def process(self, **kwargs):  # noqa: ANN003
        self.seen = kwargs
        return PostTurnResult()


def _msg(role: MessageRole, content: str, at: datetime) -> Message:
    return Message(role=role, content=content, created_at=at)


async def _chat_service_with(
    processor,
) -> tuple[ChatService, CharacterService, InMemoryConversationRepository]:
    character_repository = InMemoryCharacterRepository()
    conversation_repository = InMemoryConversationRepository()
    memory_repository = InMemoryMemoryRepository()
    registry = InMemoryChatModelRegistry(default_provider_id="fake")
    registry.register(FakeChatModel(provider_id="fake"))
    chat_service = ChatService(
        character_repository=character_repository,
        conversation_repository=conversation_repository,
        memory_repository=memory_repository,
        post_turn_processor=processor,
        prompt_context_builder=DefaultPromptContextBuilder(),
        model_registry=registry,
        state_engine=SimpleStateEngine(),
    )
    character_service = CharacterService(
        character_repository,
        conversation_repository=conversation_repository,
        memory_repository=memory_repository,
    )
    return chat_service, character_service, conversation_repository


async def _seed_conversation(
    character_service: CharacterService,
    conversation_repository: InMemoryConversationRepository,
) -> str:
    created = await character_service.create_character(
        CreateCharacterRequest(name="Mio", personality=["kind"], interests=[]),
    )
    conv = Conversation(
        id="conv-x", character_id=created.id, messages=[
            _msg(MessageRole.USER, "prior-u", NOW - timedelta(minutes=4)),
            _msg(MessageRole.ASSISTANT, "prior-a", NOW - timedelta(minutes=3)),
            _msg(MessageRole.USER, "今天天氣真好", NOW - timedelta(minutes=2)),
            _msg(MessageRole.ASSISTANT, "對啊，要出去走走嗎", NOW - timedelta(minutes=1)),
        ],
    )
    await conversation_repository.save(conv)
    return created.id


async def test_rebuild_reconstructs_turn_text_by_index() -> None:
    processor = _SpyPostTurnProcessor()
    chat_service, character_service, conv_repo = await _chat_service_with(processor)
    character_id = await _seed_conversation(character_service, conv_repo)

    result = await chat_service.run_post_turn_for_record(
        turn_record_id="turn-1",
        conversation_id="conv-x",
        character_id=character_id,
        assistant_index=3,
        persona_enabled=True,
        content_mode="normal",
        now=NOW,
    )

    assert "post_turn_skipped" not in result
    assert processor.seen["user_message"] == "今天天氣真好"
    assert processor.seen["assistant_message"] == "對啊，要出去走走嗎"
    # prior window is the pre-turn cross-source tail, cut BEFORE the turn's user msg.
    recent = [m.content for m in processor.seen["recent_messages"]]
    assert recent == ["prior-u", "prior-a"]


async def test_rebuild_skips_missing_character() -> None:
    processor = _SpyPostTurnProcessor()
    chat_service, _, _ = await _chat_service_with(processor)

    result = await chat_service.run_post_turn_for_record(
        turn_record_id="turn-1", conversation_id="conv-x",
        character_id="ghost", assistant_index=3, now=NOW,
    )
    assert result == {"post_turn_skipped": "character_missing"}
    assert processor.seen == {}


async def test_rebuild_skips_missing_conversation() -> None:
    processor = _SpyPostTurnProcessor()
    chat_service, character_service, conv_repo = await _chat_service_with(processor)
    created = await character_service.create_character(
        CreateCharacterRequest(name="Mio", personality=["kind"], interests=[]),
    )

    result = await chat_service.run_post_turn_for_record(
        turn_record_id="turn-1", conversation_id="nope",
        character_id=created.id, assistant_index=3, now=NOW,
    )
    assert result == {"post_turn_skipped": "conversation_missing"}
    assert processor.seen == {}


async def test_rebuild_skips_out_of_range_index() -> None:
    processor = _SpyPostTurnProcessor()
    chat_service, character_service, conv_repo = await _chat_service_with(processor)
    character_id = await _seed_conversation(character_service, conv_repo)

    result = await chat_service.run_post_turn_for_record(
        turn_record_id="turn-1", conversation_id="conv-x",
        character_id=character_id, assistant_index=99, now=NOW,
    )
    assert result == {"post_turn_skipped": "turn_not_found"}
    assert processor.seen == {}


async def test_rebuild_stable_index_under_concurrent_later_turn() -> None:
    # A newer turn appended after the job was enqueued must NOT hijack the rebuild:
    # the append-only index still points at THIS turn's assistant message.
    processor = _SpyPostTurnProcessor()
    chat_service, character_service, conv_repo = await _chat_service_with(processor)
    character_id = await _seed_conversation(character_service, conv_repo)
    conv = await conv_repo.get("conv-x")
    grown = conv.append(
        _msg(MessageRole.USER, "又一句", NOW + timedelta(minutes=1)),
    ).append(
        _msg(MessageRole.ASSISTANT, "新回覆", NOW + timedelta(minutes=2)),
    )
    await conv_repo.save(grown)

    result = await chat_service.run_post_turn_for_record(
        turn_record_id="turn-1", conversation_id="conv-x",
        character_id=character_id, assistant_index=3, now=NOW,
    )
    assert "post_turn_skipped" not in result
    assert processor.seen["user_message"] == "今天天氣真好"
    assert processor.seen["assistant_message"] == "對啊，要出去走走嗎"
