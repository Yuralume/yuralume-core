"""Distributed carrier for the deferred-video poll (CV4 / §4.2 one-shot kinds).

The pipeline itself lives in :mod:`feed_video_job_service`; this module is
only how a *distributed* deployment gets it called. Three collaborators,
the same trio (and the same reasoning) as
:mod:`pending_follow_up_release`:

* :class:`FeedVideoPollEnqueuer` — mints one due-time job per observation.
  Shared by the submit point, by every poll that finds the job still
  running, and by the reconcile sweep, so all three produce the same
  idempotent key and a duplicate is a no-op.
* :class:`FeedVideoPollHandler` — the worker-side apply. Re-reads the row
  (§3.3 re-verify: a row somebody else already finished is skipped) and
  delegates to the shared service.
* :class:`FeedVideoPollReconciler` — the safety net. A pending row is
  *due* the instant its next observation is; anything still due when the
  reconcile runs has lost its job (no leader at submit time, a job that
  died with its worker) and gets a fresh one.

**Embedded (self-host) never wires any of this.** There the same service
is driven by the scheduler's in-process sweep, and on a deployment whose
video provider renders synchronously no pending row is ever written at
all — so neither carrier has anything to carry.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from kokoro_link.application.services.feed_video_job_service import (
    FeedVideoJobService,
)
from kokoro_link.contracts.background_jobs import (
    COORDINATOR_LEASE_NAME,
    BackgroundCoordinatorLeasePort,
    BackgroundJobQueuePort,
    BackgroundJobSpec,
    ClaimedJob,
)
from kokoro_link.contracts.clock import ClockPort, ensure_utc
from kokoro_link.contracts.due_jobs import FEED_VIDEO_POLL_KIND, kind_spec
from kokoro_link.contracts.feed_video_jobs import (
    PendingFeedVideo,
    PendingFeedVideoRepositoryPort,
)

_LOGGER = logging.getLogger(__name__)

_POLL_PRIORITY: int = (
    kind_spec(FEED_VIDEO_POLL_KIND).priority  # type: ignore[union-attr]
)

PAYLOAD_PENDING_ID = "pending_video_id"


def _poll_idempotency_key(record: PendingFeedVideo) -> str:
    """One key per (row, observation instant).

    ``next_poll_at`` moves forward on every reschedule, so each observation
    window mints its own key: the submit point and the reconcile agree on
    the key for the *current* window (duplicate → no-op), while the next
    window is never blocked by the previous job still sitting in a terminal
    state."""
    return f"feed-video:{record.id}:{int(ensure_utc(record.next_poll_at).timestamp())}"


class FeedVideoPollEnqueuer:
    """Enqueue one due-time poll job for a pending row (distributed-only).

    Reads the live coordinator epoch from the lease, exactly like the
    follow-up release enqueuer, because the process that submits a video
    job (a worker running ``feed_compose``) does not hold that lease. No
    live lease → no enqueue, and the reconcile catches up."""

    def __init__(
        self,
        *,
        queue: BackgroundJobQueuePort,
        coordinator_lease: BackgroundCoordinatorLeasePort,
        clock: ClockPort | None = None,
    ) -> None:
        self._queue = queue
        self._lease = coordinator_lease
        self._clock = clock

    async def enqueue(
        self, record: PendingFeedVideo, *, now: datetime | None = None,
    ) -> bool:
        """Enqueue the next observation. True iff a new job row landed.

        Never raises: a failed enqueue must not turn into a failed feed
        tick, and the reconcile treats it as "try again next sweep"."""
        resolved_now = self._resolve_now(now)
        try:
            current = await self._lease.current(COORDINATOR_LEASE_NAME)
        except Exception:
            _LOGGER.exception(
                "feed video poll enqueue: lease read failed id=%s", record.id,
            )
            return False
        if current is None:
            # No coordinator has ever held the lease → embedded / not distributed.
            return False
        _owner, epoch, lease_until = current
        if ensure_utc(lease_until) < resolved_now:
            return False
        spec = BackgroundJobSpec(
            kind=FEED_VIDEO_POLL_KIND,
            idempotency_key=_poll_idempotency_key(record),
            due_at=ensure_utc(record.next_poll_at),
            fencing_epoch=epoch,
            priority=_POLL_PRIORITY,
            operator_id=record.operator_id or None,
            character_id=record.character_id,
            # Row id ONLY (§3.1 / §11 payload red line) — the post draft and
            # the job ticket stay in the table, and the handler re-reads them.
            payload={PAYLOAD_PENDING_ID: record.id},
        )
        try:
            job_id = await self._queue.enqueue(spec, now=resolved_now)
        except Exception:
            _LOGGER.exception(
                "feed video poll enqueue failed id=%s character=%s",
                record.id, record.character_id,
            )
            return False
        return job_id is not None

    def _resolve_now(self, now: datetime | None) -> datetime:
        if now is not None:
            return ensure_utc(now)
        if self._clock is not None:
            return ensure_utc(self._clock.now())
        return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class FeedVideoPollResult:
    """Outcome of handling one poll job (observability / test assertions)."""

    executed: bool
    reason: str


class FeedVideoPollHandler:
    """Worker-side apply for a ``feed_video_poll`` job."""

    def __init__(
        self,
        *,
        service: FeedVideoJobService,
        clock: ClockPort | None = None,
    ) -> None:
        self._service = service
        self._clock = clock

    async def handle(
        self, job: ClaimedJob, *, now: datetime | None = None,
    ) -> FeedVideoPollResult:
        resolved_now = self._resolve_now(now)
        record_id = (job.payload or {}).get(PAYLOAD_PENDING_ID)
        if not isinstance(record_id, str) or not record_id:
            return FeedVideoPollResult(executed=False, reason="no_pending_id")
        outcome = await self._service.poll_by_id(record_id, now=resolved_now)
        return FeedVideoPollResult(executed=True, reason=outcome)

    def _resolve_now(self, now: datetime | None) -> datetime:
        if now is not None:
            return ensure_utc(now)
        if self._clock is not None:
            return ensure_utc(self._clock.now())
        return datetime.now(timezone.utc)


class FeedVideoPollReconciler:
    """Low-frequency safety net: re-enqueue due rows that lost their job.

    Runs on the coordinator's reconcile cadence alongside the per-kind
    chain reconciler. A pending row whose observation instant has passed
    and whose job is gone would otherwise wait forever — and, unlike a
    missed chain link, that means a composed post silently never appears.
    """

    def __init__(
        self,
        *,
        repository: PendingFeedVideoRepositoryPort,
        enqueuer: FeedVideoPollEnqueuer,
        clock: ClockPort | None = None,
        scan_limit: int = 200,
    ) -> None:
        self._repository = repository
        self._enqueuer = enqueuer
        self._clock = clock
        self._scan_limit = max(1, scan_limit)

    async def run_once(self, *, now: datetime | None = None) -> int:
        resolved_now = self._resolve_now(now)
        try:
            due = await self._repository.list_due(
                now=resolved_now, limit=self._scan_limit,
            )
        except Exception:
            _LOGGER.exception("feed video poll reconcile: list_due failed")
            return 0
        enqueued = 0
        for record in due:
            if await self._enqueuer.enqueue(record, now=resolved_now):
                enqueued += 1
        if enqueued:
            _LOGGER.info(
                "feed video poll reconcile: re-enqueued %d row(s) of %d due",
                enqueued, len(due),
            )
        return enqueued

    def _resolve_now(self, now: datetime | None) -> datetime:
        if now is not None:
            return ensure_utc(now)
        if self._clock is not None:
            return ensure_utc(self._clock.now())
        return datetime.now(timezone.utc)


__all__ = [
    "FeedVideoPollEnqueuer",
    "FeedVideoPollHandler",
    "FeedVideoPollReconciler",
    "FeedVideoPollResult",
]
