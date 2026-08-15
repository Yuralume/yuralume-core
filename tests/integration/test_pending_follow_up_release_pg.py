"""PostgreSQL integration for event-driven follow-up release (§5 priority 1).

Drives the release enqueuer / handler / reconciler against the REAL
``SABackgroundJobQueue`` + ``SABackgroundCoordinatorLease`` so the new one-shot
``pending_follow_up_release`` kind is proven on Postgres: enqueue under the current
coordinator epoch → claim (SKIP LOCKED, priority 1 first) → handler delegates to the
dispatcher → complete, with no self-chain; idempotency collapses a duplicate; the
reconcile re-enqueues a due row that has no active job.

The follow-up row store uses the in-memory repository — the SA follow-up repository
has its own PG coverage; the genuinely new-on-Postgres surface here is the queue's
handling of the one-shot kind.

Requires Docker (testcontainers); skipped automatically when unavailable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.application.services.pending_follow_up_release import (
    PendingFollowUpReleaseEnqueuer,
    PendingFollowUpReleaseHandler,
    PendingFollowUpReleaseReconciler,
)
from kokoro_link.contracts.background_jobs import (
    COORDINATOR_LEASE_NAME,
    JobOutcome,
    JobStatus,
)
from kokoro_link.contracts.due_jobs import PENDING_FOLLOW_UP_RELEASE_KIND
from kokoro_link.domain.entities.pending_follow_up import (
    PendingFollowUp,
    PendingFollowUpMessage,
)
from kokoro_link.infrastructure.persistence.sa_background_jobs import (
    SABackgroundJobQueue,
)
from kokoro_link.infrastructure.persistence.sa_background_runtime import (
    SABackgroundCoordinatorLease,
)
from kokoro_link.infrastructure.repositories.in_memory_pending_follow_ups import (
    InMemoryPendingFollowUpRepository,
)

pytestmark = pytest.mark.asyncio

BASE = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)


def _row(*, scheduled_for: datetime = BASE, cid: str = "char-1") -> PendingFollowUp:
    return PendingFollowUp.new(
        character_id=cid,
        conversation_id="conv-1",
        first_message=PendingFollowUpMessage.new(
            content="晚餐想吃什麼", queued_at=scheduled_for - timedelta(minutes=30),
        ),
        brief_reply="先回，等會議結束我再好好回你",
        defer_reason="會議中",
        scheduled_for=scheduled_for,
        now=scheduled_for - timedelta(minutes=30),
    )


@dataclass
class _SpyDispatcher:
    result: bool = True
    calls: list[str] = field(default_factory=list)

    async def release_row(self, row, *, now, defer_capabilities=False):  # noqa: ANN001
        self.calls.append(row.id)
        return self.result


async def _harness(session_factory):
    lease = SABackgroundCoordinatorLease(session_factory)
    epoch = await lease.acquire(
        COORDINATOR_LEASE_NAME, "coord", ttl_seconds=3600, now=BASE,
    )
    assert epoch is not None
    queue = SABackgroundJobQueue(session_factory)
    repo = InMemoryPendingFollowUpRepository()
    enqueuer = PendingFollowUpReleaseEnqueuer(queue=queue, coordinator_lease=lease)
    return queue, repo, enqueuer, epoch


async def test_enqueue_claim_release_on_postgres(session_factory) -> None:
    queue, repo, enqueuer, _ = await _harness(session_factory)
    row = _row()
    await repo.add(row)

    assert await enqueuer.enqueue(row, now=BASE) is True

    dispatcher = _SpyDispatcher(result=True)
    handler = PendingFollowUpReleaseHandler(repository=repo, dispatcher=dispatcher)

    claimed = await queue.claim("w1", now=BASE, limit=10, lease_seconds=120)
    release_jobs = [j for j in claimed if j.kind == PENDING_FOLLOW_UP_RELEASE_KIND]
    assert len(release_jobs) == 1
    job = release_jobs[0]
    assert job.priority == 1
    assert job.payload == {"follow_up_id": row.id}

    result = await handler.handle(job, now=BASE)
    assert result.executed is True
    assert dispatcher.calls == [row.id]
    assert await queue.complete(job.id, "w1", outcome=JobOutcome(), now=BASE)

    # One-shot: the terminal job left nothing active behind it.
    stats = await queue.stats(now=BASE)
    assert stats.status_counts.get(JobStatus.QUEUED.value, 0) == 0
    assert stats.status_counts.get(JobStatus.CLAIMED.value, 0) == 0


async def test_duplicate_enqueue_is_idempotent_on_postgres(session_factory) -> None:
    queue, repo, enqueuer, _ = await _harness(session_factory)
    row = _row()
    await repo.add(row)

    assert await enqueuer.enqueue(row, now=BASE) is True
    assert await enqueuer.enqueue(row, now=BASE) is False

    stats = await queue.stats(now=BASE)
    assert stats.kind_counts.get(PENDING_FOLLOW_UP_RELEASE_KIND) == 1


async def test_reconcile_reenqueues_due_row_on_postgres(session_factory) -> None:
    queue, repo, enqueuer, _ = await _harness(session_factory)
    await repo.add(_row(scheduled_for=BASE - timedelta(minutes=1)))
    reconciler = PendingFollowUpReleaseReconciler(repository=repo, enqueuer=enqueuer)

    assert await reconciler.run_once(now=BASE) == 1
    assert await reconciler.run_once(now=BASE) == 0  # already active → no-op

    stats = await queue.stats(now=BASE)
    assert stats.kind_counts.get(PENDING_FOLLOW_UP_RELEASE_KIND) == 1


# -- #10 at-most-once visible send across a crash (real SA slot ledger) ------- #


@dataclass
class _FakeCharacter:
    id: str = "char-1"
    user_id: str = "op1"
    frozen: bool = False
    subscription_locked: bool = False


@dataclass
class _FakeCharRepo:
    character: _FakeCharacter

    async def get(self, character_id: str):  # noqa: ANN001
        return self.character if character_id == self.character.id else None


@dataclass
class _FakeComposeOutput:
    content_text: str


class _FakeComposer:
    async def compose(self, _compose_input):  # noqa: ANN001
        return _FakeComposeOutput(content_text="等會議結束我好好回你 ❤️")


@dataclass
class _SentAttempt:
    class _Outcome:
        value = "sent"

    outcome = _Outcome()


class _CountingProactiveDispatcher:
    """Records every visible send so a duplicate is caught."""

    def __init__(self) -> None:
        self.sends: list[str] = []

    async def deliver_pre_composed(  # noqa: ANN001
        self, *, character_id, text, trigger, reason, attachments=(), now,
    ):
        self.sends.append(text)
        return _SentAttempt()


class _CrashOnResolvedRepo:
    """Wraps the in-memory repo and raises exactly once when a row is saved
    ``resolved`` — simulating a worker crash after the visible send but before the
    resolved-status commit lands (the crash window #10 targets)."""

    def __init__(self, inner: InMemoryPendingFollowUpRepository) -> None:
        self._inner = inner
        self.armed = True

    async def get(self, follow_up_id: str):
        return await self._inner.get(follow_up_id)

    async def save(self, follow_up) -> None:  # noqa: ANN001
        from kokoro_link.domain.entities.pending_follow_up import (
            PendingFollowUpStatus,
        )

        if self.armed and follow_up.status == PendingFollowUpStatus.RESOLVED:
            self.armed = False
            raise RuntimeError("simulated crash after send, before resolved-commit")
        await self._inner.save(follow_up)


async def test_reclaim_after_send_does_not_double_deliver_on_postgres(
    session_factory,
) -> None:
    from kokoro_link.application.services.pending_follow_up_dispatcher import (
        PendingFollowUpDispatcher,
    )
    from kokoro_link.domain.entities.pending_follow_up import (
        PendingFollowUpStatus,
    )
    from kokoro_link.infrastructure.persistence.sa_visible_slots import (
        SAVisibleSlotRepository,
    )

    inner_repo = InMemoryPendingFollowUpRepository()
    row = _row()
    await inner_repo.add(row)
    crash_repo = _CrashOnResolvedRepo(inner_repo)

    proactive = _CountingProactiveDispatcher()
    slot_port = SAVisibleSlotRepository(session_factory)
    dispatcher = PendingFollowUpDispatcher(
        repository=crash_repo,
        composer=_FakeComposer(),
        proactive_dispatcher=proactive,
        character_repository=_FakeCharRepo(_FakeCharacter(id=row.character_id)),
        visible_slot_port=slot_port,
    )

    # Attempt 1 — the send lands, the slot is claimed, then the resolved-commit
    # "crashes". The row is left ``resolving`` with the slot held.
    with pytest.raises(RuntimeError):
        await dispatcher.release_row(row, now=BASE)
    assert len(proactive.sends) == 1
    stuck = await inner_repo.get(row.id)
    assert stuck.status == PendingFollowUpStatus.RESOLVING

    # Attempt 2 — a reclaiming worker re-runs the SAME release. The slot claim now
    # fails (already owned by attempt 1's committed send), so it resolves WITHOUT a
    # second visible delivery.
    released = await dispatcher.release_row(stuck, now=BASE + timedelta(seconds=5))
    assert released is True
    assert len(proactive.sends) == 1  # no duplicate promise reached the player
    final = await inner_repo.get(row.id)
    assert final.status == PendingFollowUpStatus.RESOLVED


async def test_failed_delivery_releases_slot_for_retry_on_postgres(
    session_factory,
) -> None:
    """A delivery FAILURE must free the slot so a genuine retry re-sends — the
    at-most-once guard must not swallow a message that never actually went out."""
    from kokoro_link.application.services.pending_follow_up_dispatcher import (
        PendingFollowUpDispatcher,
    )
    from kokoro_link.domain.entities.pending_follow_up import (
        PendingFollowUpStatus,
    )
    from kokoro_link.infrastructure.persistence.sa_visible_slots import (
        SAVisibleSlotRepository,
    )

    class _FlakyProactive:
        def __init__(self) -> None:
            self.calls = 0

        async def deliver_pre_composed(  # noqa: ANN001
            self, *, character_id, text, trigger, reason, attachments=(), now,
        ):
            self.calls += 1
            if self.calls == 1:
                return None  # first delivery fails (binding offline)
            return _SentAttempt()

    repo = InMemoryPendingFollowUpRepository()
    row = _row()
    await repo.add(row)
    proactive = _FlakyProactive()
    dispatcher = PendingFollowUpDispatcher(
        repository=repo,
        composer=_FakeComposer(),
        proactive_dispatcher=proactive,
        character_repository=_FakeCharRepo(_FakeCharacter(id=row.character_id)),
        visible_slot_port=SAVisibleSlotRepository(session_factory),
    )

    assert await dispatcher.release_row(row, now=BASE) is False  # failed → queued
    requeued = await repo.get(row.id)
    assert requeued.status == PendingFollowUpStatus.QUEUED

    # Retry succeeds because the failed delivery released the slot.
    assert await dispatcher.release_row(requeued, now=BASE + timedelta(seconds=5)) is True
    assert proactive.calls == 2
    assert (await repo.get(row.id)).status == PendingFollowUpStatus.RESOLVED
