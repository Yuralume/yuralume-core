"""The distributed carrier for the deferred-video poll (CV4).

The pipeline's own behaviour is covered in
``test_feed_video_async_pipeline``; what is under test here is the way a
*distributed* deployment gets it called — one due-time job per
observation, minted idempotently by the submit point, by each poll and by
the reconcile sweep alike, against the full-fidelity in-memory queue and
lease so fencing and idempotency are exercised for real.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.application.services.feed_video_poll_jobs import (
    FeedVideoPollEnqueuer,
    FeedVideoPollHandler,
    FeedVideoPollReconciler,
)
from kokoro_link.contracts.background_jobs import (
    COORDINATOR_LEASE_NAME,
    ClaimedJob,
)
from kokoro_link.contracts.due_jobs import FEED_VIDEO_POLL_KIND
from kokoro_link.contracts.feed_video_jobs import PendingFeedVideo
from kokoro_link.infrastructure.repositories.in_memory_background_jobs import (
    InMemoryBackgroundCoordinatorLease,
    InMemoryBackgroundJobQueue,
)
from kokoro_link.infrastructure.repositories.in_memory_pending_feed_videos import (
    InMemoryPendingFeedVideoRepository,
)

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)


def _record(*, job_id: str = "job-1") -> PendingFeedVideo:
    return PendingFeedVideo.create(
        character_id="char-1",
        operator_id="op-1",
        job_id=job_id,
        content_text="想拍下這段路。",
        post_kind="mood",
        source_kind="silence",
        source_ref_id=None,
        first_frame_url="/uploads/feed/char-1/frame.png",
        first_frame_key="feed/char-1/frame.png",
        image_prompt="neon street",
        video_prompt="slow dolly-in",
        submitted_at=NOW,
    )


async def _leased_queue():
    lease = InMemoryBackgroundCoordinatorLease()
    queue = InMemoryBackgroundJobQueue(lease=lease)
    epoch = await lease.acquire(
        COORDINATOR_LEASE_NAME, "coord", ttl_seconds=3600, now=NOW,
    )
    assert epoch is not None
    return queue, lease


async def _poll_jobs(queue: InMemoryBackgroundJobQueue) -> int:
    stats = await queue.stats(now=NOW)
    return stats.kind_counts.get(FEED_VIDEO_POLL_KIND, 0)


class _SpyService:
    def __init__(self, outcome: str = "published") -> None:
        self.outcome = outcome
        self.calls: list[str] = []

    async def poll_by_id(self, record_id: str, *, now: datetime) -> str:
        del now
        self.calls.append(record_id)
        return self.outcome


def _claimed(
    record: PendingFeedVideo, *, payload: dict | None = None,
) -> ClaimedJob:
    return ClaimedJob(
        id="job-row-1", kind=FEED_VIDEO_POLL_KIND, attempt_count=1,
        max_attempts=5, fencing_epoch=1,
        idempotency_key=f"feed-video:{record.id}:0",
        priority=4, due_at=record.next_poll_at, payload_version=1,
        payload=(
            {"pending_video_id": record.id} if payload is None else payload
        ),
        lease_owner="w1",
        lease_until=NOW + timedelta(seconds=120),
        character_id=record.character_id,
    )


# --- enqueuer --------------------------------------------------------------


async def test_enqueue_carries_the_row_id_and_nothing_else() -> None:
    queue, lease = await _leased_queue()
    enqueuer = FeedVideoPollEnqueuer(queue=queue, coordinator_lease=lease)
    record = _record()

    assert await enqueuer.enqueue(record, now=NOW) is True

    claimed = await queue.claim(
        "w1", now=NOW + timedelta(hours=1), limit=10, lease_seconds=120,
    )
    job = next(j for j in claimed if j.kind == FEED_VIDEO_POLL_KIND)
    assert job.due_at == record.next_poll_at
    assert job.priority == 4
    assert job.character_id == "char-1"
    # The draft, the frame and the job ticket all stay in the table.
    assert job.payload == {"pending_video_id": record.id}
    assert "想拍" not in str(job.payload)


async def test_enqueue_is_idempotent_within_one_observation_window() -> None:
    """The submit point and the reconcile both mint the job for the *same*
    window, so the second call must collapse — but the next window (after a
    reschedule) is a different observation and must be allowed through."""
    queue, lease = await _leased_queue()
    enqueuer = FeedVideoPollEnqueuer(queue=queue, coordinator_lease=lease)
    record = _record()

    assert await enqueuer.enqueue(record, now=NOW) is True
    assert await enqueuer.enqueue(record, now=NOW) is False
    assert await _poll_jobs(queue) == 1

    moved = record.with_next_poll(
        next_poll_at=record.next_poll_at + timedelta(minutes=1), now=NOW,
    )
    assert await enqueuer.enqueue(moved, now=NOW) is True
    assert await _poll_jobs(queue) == 2


async def test_enqueue_noop_without_a_leader() -> None:
    """Embedded / self-host: no coordinator ever held the lease, so there is
    nothing to enqueue onto — the in-process sweep is the carrier there."""
    lease = InMemoryBackgroundCoordinatorLease()
    queue = InMemoryBackgroundJobQueue(lease=lease)
    enqueuer = FeedVideoPollEnqueuer(queue=queue, coordinator_lease=lease)

    assert await enqueuer.enqueue(_record(), now=NOW) is False
    assert await _poll_jobs(queue) == 0


# --- handler ---------------------------------------------------------------


async def test_handler_delegates_to_the_shared_pipeline() -> None:
    service = _SpyService(outcome="degraded")
    handler = FeedVideoPollHandler(service=service)
    record = _record()

    result = await handler.handle(_claimed(record), now=NOW)

    assert result.executed is True
    assert result.reason == "degraded"
    assert service.calls == [record.id]


async def test_handler_ignores_a_job_without_a_row_id() -> None:
    service = _SpyService()
    handler = FeedVideoPollHandler(service=service)
    record = _record()

    result = await handler.handle(_claimed(record, payload={}), now=NOW)

    assert result.executed is False
    assert result.reason == "no_pending_id"
    assert service.calls == []


# --- reconciler ------------------------------------------------------------


async def test_reconcile_re_enqueues_a_due_row_that_lost_its_job() -> None:
    """The difference between a late post and a missing one: a lost chain
    link costs one delayed tick, a lost poll costs the whole post."""
    queue, lease = await _leased_queue()
    repository = InMemoryPendingFeedVideoRepository()
    record = _record()
    await repository.add(record)
    reconciler = FeedVideoPollReconciler(
        repository=repository,
        enqueuer=FeedVideoPollEnqueuer(queue=queue, coordinator_lease=lease),
    )

    due_at = record.next_poll_at + timedelta(seconds=1)
    assert await reconciler.run_once(now=due_at) == 1
    assert await _poll_jobs(queue) == 1
    # Second sweep inside the same window is a no-op (the job is still active).
    assert await reconciler.run_once(now=due_at) == 0
    assert await _poll_jobs(queue) == 1


async def test_reconcile_ignores_rows_that_already_finished() -> None:
    queue, lease = await _leased_queue()
    repository = InMemoryPendingFeedVideoRepository()
    record = _record()
    await repository.add(record)
    await repository.finish_if_pending(
        record.id, status="published", now=NOW, post_id="post-1",
    )
    reconciler = FeedVideoPollReconciler(
        repository=repository,
        enqueuer=FeedVideoPollEnqueuer(queue=queue, coordinator_lease=lease),
    )

    assert await reconciler.run_once(
        now=record.next_poll_at + timedelta(minutes=5),
    ) == 0
    assert await _poll_jobs(queue) == 0
