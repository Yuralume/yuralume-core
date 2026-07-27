"""PostgreSQL integration for event-driven post-turn processing (§5 priority 3).

Drives the post-turn enqueuer + handler through the runner against the REAL
``SABackgroundJobQueue`` + ``SABackgroundCoordinatorLease`` so the new one-shot
``post_turn`` kind is proven on Postgres: enqueue under the current coordinator epoch
→ claim (SKIP LOCKED, ids-only payload) → runner routes to the handler → complete,
with no self-chain; a duplicate enqueue collapses on the idempotency key; a re-claim
after completion finds nothing left (one-shot).

The post-turn body itself has dedicated in-memory coverage; the genuinely
new-on-Postgres surface here is the queue's handling of the one-shot kind, so the
runner uses a spy handler.

Requires Docker (testcontainers); skipped automatically when unavailable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.application.services.execution_mode_runner import (
    ExecutionModeRunner,
)
from kokoro_link.application.services.post_turn_runner import (
    PostTurnEnqueueOutcome,
    PostTurnEnqueuer,
    PostTurnHandlerResult,
)
from kokoro_link.contracts.background_jobs import (
    COORDINATOR_LEASE_NAME,
    JobOutcome,
    JobStatus,
)
from kokoro_link.contracts.due_jobs import POST_TURN_KIND
from kokoro_link.infrastructure.persistence.sa_background_jobs import (
    SABackgroundJobQueue,
)
from kokoro_link.infrastructure.persistence.sa_background_runtime import (
    SABackgroundCoordinatorLease,
)

pytestmark = pytest.mark.asyncio

BASE = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)


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


@dataclass
class _SpyHandler:
    result: PostTurnHandlerResult
    jobs: list[str] = field(default_factory=list)

    async def handle(self, job, *, now=None):  # noqa: ANN001
        self.jobs.append(job.id)
        return self.result


def _runner(queue, handler) -> ExecutionModeRunner:
    return ExecutionModeRunner(
        queue=queue,
        character_repository=None,
        character_tick_executor=None,
        social_tick_executor=None,
        gate_service=None,
        cursor=None,
        bucket_seconds=300,
        heartbeat_interval_seconds=3600,
        post_turn_handler=handler,
    )


async def _harness(session_factory):
    lease = SABackgroundCoordinatorLease(session_factory)
    epoch = await lease.acquire(
        COORDINATOR_LEASE_NAME, "coord", ttl_seconds=3600, now=BASE,
    )
    assert epoch is not None
    queue = SABackgroundJobQueue(session_factory)
    enqueuer = PostTurnEnqueuer(queue=queue, coordinator_lease=lease)
    return queue, enqueuer, epoch


async def test_enqueue_claim_execute_on_postgres(session_factory) -> None:
    queue, enqueuer, _ = await _harness(session_factory)

    assert await enqueuer.enqueue(**_enqueue_kwargs(), now=BASE) is (
        PostTurnEnqueueOutcome.INSERTED
    )

    spy = _SpyHandler(PostTurnHandlerResult(executed=True, reason="executed"))
    runner = _runner(queue, spy)

    claimed = await queue.claim("w1", now=BASE, limit=10, lease_seconds=120)
    post_turn_jobs = [j for j in claimed if j.kind == POST_TURN_KIND]
    assert len(post_turn_jobs) == 1
    job = post_turn_jobs[0]
    assert job.priority == 3
    assert job.payload["turn_record_id"] == "turn-1"
    assert job.payload["assistant_index"] == 3
    # Payload red line: only ids / flags / an enum survive the round-trip.
    assert set(job.payload.keys()) == {
        "turn_record_id", "conversation_id", "character_id",
        "assistant_index", "persona_enabled", "content_mode",
    }

    disposition = await runner.execute(
        job, would_run=False, skip_reason="frozen",
        now=BASE, worker_id="w1", lease_seconds=120,
    )
    assert disposition.action == "complete"
    assert spy.jobs == [job.id]
    assert await queue.complete(job.id, "w1", outcome=JobOutcome(), now=BASE)

    # One-shot: the terminal job left nothing active behind it.
    stats = await queue.stats(now=BASE)
    assert stats.status_counts.get(JobStatus.QUEUED.value, 0) == 0
    assert stats.status_counts.get(JobStatus.CLAIMED.value, 0) == 0


async def test_duplicate_enqueue_is_idempotent_on_postgres(session_factory) -> None:
    queue, enqueuer, _ = await _harness(session_factory)

    assert await enqueuer.enqueue(**_enqueue_kwargs(), now=BASE) is (
        PostTurnEnqueueOutcome.INSERTED
    )
    # A duplicate under a live leader is ALREADY_ACTIVE (a worker owns it), not the
    # old ambiguous ``False`` that drove a double in-process run (#11).
    assert await enqueuer.enqueue(**_enqueue_kwargs(), now=BASE) is (
        PostTurnEnqueueOutcome.ALREADY_ACTIVE
    )

    stats = await queue.stats(now=BASE)
    assert stats.kind_counts.get(POST_TURN_KIND) == 1


async def test_reclaim_after_complete_finds_nothing(session_factory) -> None:
    # One-shot rerun guard: once completed, a later claim sees no post-turn job.
    queue, enqueuer, _ = await _harness(session_factory)
    await enqueuer.enqueue(**_enqueue_kwargs(), now=BASE)

    [job] = await queue.claim("w1", now=BASE, limit=10, lease_seconds=120)
    assert await queue.complete(job.id, "w1", outcome=JobOutcome(), now=BASE)

    later = await queue.claim("w2", now=BASE + timedelta(minutes=10), limit=10, lease_seconds=120)
    assert [j for j in later if j.kind == POST_TURN_KIND] == []
