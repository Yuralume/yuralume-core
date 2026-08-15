"""Unit tests for event-driven pending-follow-up release (HOSTED_CORE_SCALING §5 priority 1).

Covers the three collaborators (enqueuer / handler / reconciler) plus the runner
routing, against the full-fidelity in-memory queue + lease so the idempotency,
fencing and one-shot (no self-chain) semantics are exercised for real.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.application.services.execution_mode_runner import (
    ExecutionModeRunner,
)
from kokoro_link.application.services.pending_follow_up_release import (
    PendingFollowUpReleaseEnqueuer,
    PendingFollowUpReleaseHandler,
    PendingFollowUpReleaseReconciler,
    ReleaseHandlerResult,
)
from kokoro_link.contracts.background_jobs import (
    COORDINATOR_LEASE_NAME,
    ClaimedJob,
    JobOutcome,
    JobStatus,
)
from kokoro_link.contracts.due_jobs import PENDING_FOLLOW_UP_RELEASE_KIND
from kokoro_link.domain.entities.pending_follow_up import (
    PendingFollowUp,
    PendingFollowUpMessage,
)
from kokoro_link.infrastructure.repositories.in_memory_background_jobs import (
    InMemoryBackgroundCoordinatorLease,
    InMemoryBackgroundJobQueue,
)
from kokoro_link.infrastructure.repositories.in_memory_pending_follow_ups import (
    InMemoryPendingFollowUpRepository,
)

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)


def _row(*, scheduled_for: datetime = NOW, character_id: str = "char-1") -> PendingFollowUp:
    return PendingFollowUp.new(
        character_id=character_id,
        conversation_id="conv-1",
        first_message=PendingFollowUpMessage.new(
            content="晚餐想吃什麼", queued_at=scheduled_for - timedelta(minutes=30),
        ),
        brief_reply="先回，等會議結束我再好好回你",
        defer_reason="會議中",
        scheduled_for=scheduled_for,
        now=scheduled_for - timedelta(minutes=30),
    )


async def _leased_queue() -> tuple[InMemoryBackgroundJobQueue, InMemoryBackgroundCoordinatorLease, int]:
    lease = InMemoryBackgroundCoordinatorLease()
    queue = InMemoryBackgroundJobQueue(lease=lease)
    epoch = await lease.acquire(
        COORDINATOR_LEASE_NAME, "coord", ttl_seconds=3600, now=NOW,
    )
    assert epoch is not None
    return queue, lease, epoch


async def _active_release_jobs(queue: InMemoryBackgroundJobQueue) -> int:
    stats = await queue.stats(now=NOW)
    return stats.kind_counts.get(PENDING_FOLLOW_UP_RELEASE_KIND, 0)


# --------------------------------------------------------------------------- #
# Enqueuer
# --------------------------------------------------------------------------- #

async def test_enqueue_creates_priority_one_release_job() -> None:
    queue, lease, _ = await _leased_queue()
    enqueuer = PendingFollowUpReleaseEnqueuer(queue=queue, coordinator_lease=lease)
    row = _row(scheduled_for=NOW + timedelta(minutes=45))

    assert await enqueuer.enqueue(row, now=NOW) is True

    stats = await queue.stats(now=NOW)
    assert stats.kind_counts.get(PENDING_FOLLOW_UP_RELEASE_KIND) == 1
    claimed = await queue.claim("w1", now=NOW + timedelta(hours=1), limit=10, lease_seconds=120)
    job = next(j for j in claimed if j.kind == PENDING_FOLLOW_UP_RELEASE_KIND)
    assert job.priority == 1
    assert job.due_at == row.scheduled_for
    assert job.payload == {"follow_up_id": row.id}
    assert job.character_id == "char-1"
    assert "chat" not in str(job.payload).lower()  # ids only, no content


async def test_enqueue_noop_without_leader() -> None:
    # No coordinator has ever acquired the lease → current() is None → embedded.
    lease = InMemoryBackgroundCoordinatorLease()
    queue = InMemoryBackgroundJobQueue(lease=lease)
    enqueuer = PendingFollowUpReleaseEnqueuer(queue=queue, coordinator_lease=lease)

    assert await enqueuer.enqueue(_row(), now=NOW) is False
    assert await _active_release_jobs(queue) == 0


async def test_enqueue_noop_when_lease_expired() -> None:
    lease = InMemoryBackgroundCoordinatorLease()
    queue = InMemoryBackgroundJobQueue(lease=lease)
    await lease.acquire(COORDINATOR_LEASE_NAME, "coord", ttl_seconds=30, now=NOW)
    enqueuer = PendingFollowUpReleaseEnqueuer(queue=queue, coordinator_lease=lease)

    # now is past the lease expiry → the enqueue would fence out → skip.
    later = NOW + timedelta(minutes=5)
    assert await enqueuer.enqueue(_row(scheduled_for=later), now=later) is False
    assert await _active_release_jobs(queue) == 0


async def test_enqueue_is_idempotent_per_row() -> None:
    queue, lease, _ = await _leased_queue()
    enqueuer = PendingFollowUpReleaseEnqueuer(queue=queue, coordinator_lease=lease)
    row = _row()

    assert await enqueuer.enqueue(row, now=NOW) is True
    assert await enqueuer.enqueue(row, now=NOW) is False  # duplicate → active idem
    assert await _active_release_jobs(queue) == 1


# --------------------------------------------------------------------------- #
# Handler
# --------------------------------------------------------------------------- #

@dataclass
class _SpyDispatcher:
    result: bool = True
    calls: list[str] = field(default_factory=list)
    defer_flags: list[bool] = field(default_factory=list)

    async def release_row(self, row, *, now, defer_capabilities=False):  # noqa: ANN001
        self.calls.append(row.id)
        self.defer_flags.append(defer_capabilities)
        return self.result


def _claimed_release(
    row: PendingFollowUp, kind: str = PENDING_FOLLOW_UP_RELEASE_KIND,
) -> ClaimedJob:
    return ClaimedJob(
        id="job-1", kind=kind, attempt_count=1,
        max_attempts=5, fencing_epoch=1, idempotency_key=f"follow_up:{row.id}:0",
        priority=1, due_at=row.scheduled_for, payload_version=1,
        payload={"follow_up_id": row.id}, lease_owner="w1",
        lease_until=NOW + timedelta(seconds=120), character_id=row.character_id,
    )


async def test_handler_delegates_to_dispatcher() -> None:
    repo = InMemoryPendingFollowUpRepository()
    row = _row()
    await repo.add(row)
    dispatcher = _SpyDispatcher(result=True)
    handler = PendingFollowUpReleaseHandler(repository=repo, dispatcher=dispatcher)

    result = await handler.handle(_claimed_release(row), now=NOW)

    assert result == ReleaseHandlerResult(executed=True, reason="released")
    assert dispatcher.calls == [row.id]


async def test_handler_skips_missing_row() -> None:
    repo = InMemoryPendingFollowUpRepository()  # never added
    dispatcher = _SpyDispatcher()
    handler = PendingFollowUpReleaseHandler(repository=repo, dispatcher=dispatcher)

    result = await handler.handle(_claimed_release(_row()), now=NOW)

    assert result.executed is False and result.reason == "row_missing"
    assert dispatcher.calls == []  # a cancelled / merged-away row does nothing


async def test_handler_skips_cancelled_row() -> None:
    repo = InMemoryPendingFollowUpRepository()
    row = _row()
    await repo.add(row.cancelled(now=NOW))  # cancel path already ran
    dispatcher = _SpyDispatcher()
    handler = PendingFollowUpReleaseHandler(repository=repo, dispatcher=dispatcher)

    result = await handler.handle(_claimed_release(row), now=NOW)

    assert result.executed is False and result.reason == "status:cancelled"
    assert dispatcher.calls == []


async def test_handler_skips_resolved_row() -> None:
    repo = InMemoryPendingFollowUpRepository()
    row = _row()
    await repo.add(row.marked_resolved(message_text="done", now=NOW))
    dispatcher = _SpyDispatcher()
    handler = PendingFollowUpReleaseHandler(repository=repo, dispatcher=dispatcher)

    result = await handler.handle(_claimed_release(row), now=NOW)

    assert result.executed is False and result.reason == "status:resolved"
    assert dispatcher.calls == []


async def test_handler_is_one_shot_no_self_chain() -> None:
    # A real queue + lease: enqueue → claim → handle → complete. The handler has
    # no queue dependency, so it structurally cannot enqueue a follow-on job.
    queue, lease, _ = await _leased_queue()
    repo = InMemoryPendingFollowUpRepository()
    row = _row()
    await repo.add(row)
    enqueuer = PendingFollowUpReleaseEnqueuer(queue=queue, coordinator_lease=lease)
    await enqueuer.enqueue(row, now=NOW)
    handler = PendingFollowUpReleaseHandler(
        repository=repo, dispatcher=_SpyDispatcher(result=True),
    )

    [claimed] = await queue.claim("w1", now=NOW, limit=10, lease_seconds=120)
    result = await handler.handle(claimed, now=NOW)
    assert result.executed is True
    assert await queue.complete(claimed.id, "w1", outcome=JobOutcome(), now=NOW)

    # No new release job was chained: the only one is now terminal.
    stats = await queue.stats(now=NOW)
    assert stats.status_counts.get(JobStatus.QUEUED.value, 0) == 0
    assert stats.status_counts.get(JobStatus.CLAIMED.value, 0) == 0


# --------------------------------------------------------------------------- #
# Reconciler
# --------------------------------------------------------------------------- #

async def test_reconcile_reenqueues_missing_due_row() -> None:
    queue, lease, _ = await _leased_queue()
    repo = InMemoryPendingFollowUpRepository()
    await repo.add(_row(scheduled_for=NOW - timedelta(minutes=1)))  # due, no job yet
    enqueuer = PendingFollowUpReleaseEnqueuer(queue=queue, coordinator_lease=lease)
    reconciler = PendingFollowUpReleaseReconciler(repository=repo, enqueuer=enqueuer)

    assert await reconciler.run_once(now=NOW) == 1
    assert await _active_release_jobs(queue) == 1
    # Second pass is a no-op — the row already has an active job.
    assert await reconciler.run_once(now=NOW) == 0
    assert await _active_release_jobs(queue) == 1


async def test_reconcile_skips_already_enqueued_row() -> None:
    queue, lease, _ = await _leased_queue()
    repo = InMemoryPendingFollowUpRepository()
    row = _row(scheduled_for=NOW - timedelta(minutes=1))
    await repo.add(row)
    enqueuer = PendingFollowUpReleaseEnqueuer(queue=queue, coordinator_lease=lease)
    await enqueuer.enqueue(row, now=NOW)  # event already fired at write time
    reconciler = PendingFollowUpReleaseReconciler(repository=repo, enqueuer=enqueuer)

    assert await reconciler.run_once(now=NOW) == 0
    assert await _active_release_jobs(queue) == 1


async def test_reconcile_rescues_stale_resolving_orphan() -> None:
    """A ``resolving`` row left by a crashed worker (never terminal) is rescued:
    ``list_due`` returns only ``queued`` rows, so without this sweep the orphan
    would leak forever. A FRESH ``resolving`` row (an in-flight release) is left
    alone."""
    queue, lease, _ = await _leased_queue()
    repo = InMemoryPendingFollowUpRepository()

    # Orphan: marked resolving 20 min ago and never finished.
    orphan = _row(scheduled_for=NOW - timedelta(hours=1), character_id="char-1")
    await repo.add(orphan.marked_resolving(now=NOW - timedelta(minutes=20)))
    # Fresh in-flight: marked resolving 5 s ago — a live worker still owns it.
    fresh = _row(scheduled_for=NOW - timedelta(hours=1), character_id="char-2")
    await repo.add(fresh.marked_resolving(now=NOW - timedelta(seconds=5)))

    enqueuer = PendingFollowUpReleaseEnqueuer(queue=queue, coordinator_lease=lease)
    reconciler = PendingFollowUpReleaseReconciler(repository=repo, enqueuer=enqueuer)

    assert await reconciler.run_once(now=NOW) == 1  # only the stale orphan
    assert await _active_release_jobs(queue) == 1
    keys = await queue.active_chain_keys()
    assert ("pending_follow_up_release", "char-1") in keys
    assert ("pending_follow_up_release", "char-2") not in keys


async def test_list_stale_resolving_thresholds() -> None:
    repo = InMemoryPendingFollowUpRepository()
    await repo.add(_row(character_id="q").marked_resolving(now=NOW - timedelta(minutes=20)))
    await repo.add(_row(character_id="fresh").marked_resolving(now=NOW - timedelta(seconds=1)))
    # A queued (not resolving) row is never returned by this sweep.
    await repo.add(_row(character_id="queued"))

    stale = await repo.list_stale_resolving(now=NOW, older_than_seconds=600)
    ids = {r.character_id for r in stale}
    assert ids == {"q"}


async def test_reconcile_noop_without_leader() -> None:
    lease = InMemoryBackgroundCoordinatorLease()  # never acquired
    queue = InMemoryBackgroundJobQueue(lease=lease)
    repo = InMemoryPendingFollowUpRepository()
    await repo.add(_row(scheduled_for=NOW - timedelta(minutes=1)))
    enqueuer = PendingFollowUpReleaseEnqueuer(queue=queue, coordinator_lease=lease)
    reconciler = PendingFollowUpReleaseReconciler(repository=repo, enqueuer=enqueuer)

    assert await reconciler.run_once(now=NOW) == 0
    assert await _active_release_jobs(queue) == 0


# --------------------------------------------------------------------------- #
# Runner routing
# --------------------------------------------------------------------------- #

@dataclass
class _SpyHandler:
    result: ReleaseHandlerResult
    jobs: list[str] = field(default_factory=list)

    async def handle(self, job, *, now=None):  # noqa: ANN001
        self.jobs.append(job.id)
        return self.result


def _runner(queue, release_handler) -> ExecutionModeRunner:
    return ExecutionModeRunner(
        queue=queue,
        character_repository=None,
        character_tick_executor=None,
        social_tick_executor=None,
        gate_service=None,
        cursor=None,
        bucket_seconds=300,
        heartbeat_interval_seconds=3600,  # never fires within the test body
        pending_follow_up_release_handler=release_handler,
    )


async def test_runner_routes_release_kind_even_when_would_run_false() -> None:
    queue = InMemoryBackgroundJobQueue()
    row = _row()
    spy = _SpyHandler(ReleaseHandlerResult(executed=True, reason="released"))
    runner = _runner(queue, spy)

    disposition = await runner.execute(
        _claimed_release(row),
        would_run=False,  # per-character gate says skip — release ignores it
        skip_reason="frozen",
        now=NOW, worker_id="w1", lease_seconds=120,
    )

    assert disposition.action == "complete"
    assert disposition.outcome["executed"] is True
    assert disposition.outcome["kind"] == PENDING_FOLLOW_UP_RELEASE_KIND
    assert spy.jobs == ["job-1"]


async def test_runner_release_handler_unwired_fails_retry() -> None:
    queue = InMemoryBackgroundJobQueue()
    runner = _runner(queue, None)

    disposition = await runner.execute(
        _claimed_release(_row()),
        would_run=True, skip_reason=None,
        now=NOW, worker_id="w1", lease_seconds=120,
    )

    assert disposition.action == "fail_retry"
    assert isinstance(disposition.error, RuntimeError)
    assert "handler_unwired" in str(disposition.error)
