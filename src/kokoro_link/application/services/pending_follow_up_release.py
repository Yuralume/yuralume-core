"""Event-driven release of pending follow-ups (HOSTED_CORE_SCALING §5 priority 1).

Phase 3 released busy-defer / scheduled-promise follow-ups by scanning
``list_due`` on *every* distributed social tick (a whole-table cursor walk). A
follow-up's release instant (``PendingFollowUp.scheduled_for``) is precisely known
the moment the row is written, so Phase 5 flips it to **event-driven**: the chat
write point enqueues one priority-1 one-shot job due at exactly that instant, and
the coordinator's low-frequency reconcile is only a safety net for missed events.

Three small collaborators live here — the whole feature is one cohesive unit:

* :class:`PendingFollowUpReleaseEnqueuer` — the single enqueue path, shared by the
  chat write points and the reconcile. It reads the *current* coordinator epoch via
  the lease port (the API process never holds the coordinator lease) and enqueues
  under it; when no live lease exists (no leader / embedded self-host) it does
  nothing and leaves the reconcile to catch up. The queue's active-idempotency index
  (``follow_up:{id}:{window}``) makes a duplicate enqueue a no-op, so the write point
  and the reconcile can both call it safely.
* :class:`PendingFollowUpReleaseHandler` — the worker-side apply. It re-verifies the
  row still exists and is still due (§3.3 re-verify: a cancelled / merged / resolved
  row is skipped, so a stale queue row never double-sends) and delegates the actual
  compose + fan-out to the existing :class:`PendingFollowUpDispatcher`.
* :class:`PendingFollowUpReleaseReconciler` — the reconcile safety net: sweep the
  currently-due rows and (idempotently) re-enqueue any that lost their event job.

**Distributed-only by construction** — the embedded (self-host) scheduler never
wires any of this; its follow-up release stays the in-process ``ProactiveScheduler``
tick, byte-identical (§ red line).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from kokoro_link.contracts.background_jobs import (
    COORDINATOR_LEASE_NAME,
    BackgroundCoordinatorLeasePort,
    BackgroundJobQueuePort,
    BackgroundJobSpec,
    ClaimedJob,
)
from kokoro_link.contracts.clock import ClockPort, ensure_utc
from kokoro_link.contracts.due_jobs import (
    PENDING_FOLLOW_UP_RELEASE_KIND,
    kind_spec,
)
from kokoro_link.contracts.pending_follow_up import (
    PendingFollowUpRepositoryPort,
)
from kokoro_link.domain.entities.pending_follow_up import (
    PendingFollowUp,
    PendingFollowUpStatus,
)

_LOGGER = logging.getLogger(__name__)

#: Statuses for which a release job still has work to do. ``queued`` is the normal
#: case; ``resolving`` is a dangling row from an earlier crashed release (the retry
#: re-attempts it). ``resolved`` / ``cancelled`` are terminal → the job skips.
_RELEASABLE_STATUSES: frozenset[str] = frozenset({
    PendingFollowUpStatus.QUEUED.value,
    PendingFollowUpStatus.RESOLVING.value,
})

_RELEASE_PRIORITY: int = (
    kind_spec(PENDING_FOLLOW_UP_RELEASE_KIND).priority  # type: ignore[union-attr]
)


def _release_window(scheduled_for: datetime) -> int:
    """Idempotency window derived from the row's release instant.

    ``scheduled_for`` is fixed for the life of a row (merge keeps it, cancel
    terminates the row), so both the write-point enqueue and the reconcile compute
    the SAME ``follow_up:{id}:{window}`` key → the queue accepts at most one active
    release job per row. A later occurrence of the same row id (a failed release
    flipped back to ``queued`` after its job reached a terminal state) reuses the
    same key, which is accepted only because the previous job is no longer active."""
    return int(ensure_utc(scheduled_for).timestamp())


def _release_idempotency_key(row: PendingFollowUp) -> str:
    return f"follow_up:{row.id}:{_release_window(row.scheduled_for)}"


class PendingFollowUpReleaseEnqueuer:
    """Enqueue one due-time release job for a follow-up row (distributed-only).

    The API process does not hold the coordinator lease, so the fencing epoch is
    read from the lease's ``current()`` — the true epoch of whatever coordinator is
    live. No live lease (no leader / embedded self-host) → no enqueue; the reconcile
    is the fallback. The enqueue itself fences atomically against the epoch, so even
    a briefly-stale read can never insert under a superseded coordinator."""

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
        self, row: PendingFollowUp, *, now: datetime | None = None,
    ) -> bool:
        """Enqueue the release job. Returns True iff a new job row was inserted.

        Never raises: a failure on the foreground chat path must not break the turn,
        and the reconcile treats a False as "try again next sweep". A no-op (no
        leader, stale lease, or an already-active duplicate) also returns False."""
        resolved_now = self._resolve_now(now)
        try:
            current = await self._lease.current(COORDINATOR_LEASE_NAME)
        except Exception:
            _LOGGER.exception(
                "follow-up release enqueue: lease read failed id=%s", row.id,
            )
            return False
        if current is None:
            # No coordinator has ever held the lease → embedded / not distributed.
            return False
        _owner, epoch, lease_until = current
        if ensure_utc(lease_until) < resolved_now:
            # The last leader's lease has expired; the enqueue would fence out.
            # Leave it — the next leader's reconcile re-enqueues the still-due row.
            return False
        spec = BackgroundJobSpec(
            kind=PENDING_FOLLOW_UP_RELEASE_KIND,
            idempotency_key=_release_idempotency_key(row),
            due_at=ensure_utc(row.scheduled_for),
            fencing_epoch=epoch,
            priority=_RELEASE_PRIORITY,
            character_id=row.character_id,
            # Payload carries the row id ONLY (no chat content — §3.1 / §11); the
            # handler re-loads the row and re-verifies it before releasing.
            payload={"follow_up_id": row.id},
        )
        try:
            job_id = await self._queue.enqueue(spec, now=resolved_now)
        except Exception:
            _LOGGER.exception(
                "follow-up release enqueue failed id=%s character=%s",
                row.id, row.character_id,
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
class ReleaseHandlerResult:
    """Outcome of handling one release job (observability / test assertions)."""

    executed: bool
    reason: str


class PendingFollowUpReleaseHandler:
    """Worker-side apply for a ``pending_follow_up_release`` job.

    Re-verifies the row (§3.3) then delegates to the existing dispatcher. The
    dispatcher owns busy-score / frozen re-gating and the compose + fan-out; a
    still-busy or frozen character leaves the row queued (``executed=False``) and the
    reconcile re-enqueues it later — the job is one-shot, never a self-chain."""

    def __init__(
        self,
        *,
        repository: PendingFollowUpRepositoryPort,
        dispatcher,  # noqa: ANN001 - PendingFollowUpDispatcher (avoid import cycle)
        clock: ClockPort | None = None,
    ) -> None:
        self._repository = repository
        self._dispatcher = dispatcher
        self._clock = clock

    async def handle(
        self, job: ClaimedJob, *, now: datetime | None = None,
    ) -> ReleaseHandlerResult:
        resolved_now = self._resolve_now(now)
        follow_up_id = (job.payload or {}).get("follow_up_id")
        if not isinstance(follow_up_id, str) or not follow_up_id:
            return ReleaseHandlerResult(executed=False, reason="no_follow_up_id")
        try:
            row = await self._repository.get(follow_up_id)
        except Exception:
            _LOGGER.exception(
                "follow-up release: row load failed id=%s", follow_up_id,
            )
            # Transient: fail-retry so a reclaim tries again (the release is idempotent
            # at the row-status level, so a retry can never double-send).
            raise
        if row is None:
            # Cancelled / merged-away / deleted between enqueue and claim — the
            # single active job simply has nothing to do (§3.3 cancel semantics).
            return ReleaseHandlerResult(executed=False, reason="row_missing")
        if row.status.value not in _RELEASABLE_STATUSES:
            # Already resolved / cancelled → the row-status transition is the
            # visible-send dedup; a reclaim after a lost lease finds it terminal.
            return ReleaseHandlerResult(
                executed=False, reason=f"status:{row.status.value}",
            )
        if ensure_utc(row.scheduled_for) > resolved_now:
            # Not yet due (should not happen — due_at == scheduled_for — but a clock
            # skew guard keeps the release honest).
            return ReleaseHandlerResult(executed=False, reason="not_due")
        released = await self._dispatcher.release_row(row, now=resolved_now)
        return ReleaseHandlerResult(
            executed=released,
            reason="released" if released else "not_released",
        )

    def _resolve_now(self, now: datetime | None) -> datetime:
        if now is not None:
            return ensure_utc(now)
        if self._clock is not None:
            return ensure_utc(self._clock.now())
        return datetime.now(timezone.utc)


#: A ``resolving`` row still un-terminal this long after its last touch is a
#: crashed worker's orphan, not an in-flight release (a real release flips the row
#: within seconds). The distributed reconcile rescues it — re-enqueues a release job
#: so it reaches a terminal state instead of leaking forever. Generous enough that a
#: legitimately slow compose is never yanked out from under the running worker.
_RESOLVING_STALE_AFTER_SECONDS: float = 600.0


class PendingFollowUpReleaseReconciler:
    """Low-frequency safety net: re-enqueue due rows that lost their event job.

    Runs on the coordinator's reconcile cadence (~15 min) alongside the per-kind
    chain reconciler, already gated to the distributed leader. For every currently
    -due row it (idempotently) calls the shared enqueuer: a row that already has an
    active release job is a no-op; a row whose event was missed (no leader at write
    time) or whose previous release failed back to ``queued`` gets a fresh job.

    It ALSO rescues ``resolving`` orphans — rows a worker claimed but crashed before
    finishing. ``list_due`` only returns ``queued`` rows (and is shared byte-for-byte
    with the embedded tick, so it must not change), so without this sweep a
    ``resolving`` row left by a dead worker would leak forever. The rescue re-enqueues
    it; the at-most-once visible-slot claim on the release path stops a rescue from
    re-sending an already-delivered message."""

    def __init__(
        self,
        *,
        repository: PendingFollowUpRepositoryPort,
        enqueuer: PendingFollowUpReleaseEnqueuer,
        clock: ClockPort | None = None,
        scan_limit: int = 200,
        resolving_stale_after_seconds: float = _RESOLVING_STALE_AFTER_SECONDS,
    ) -> None:
        self._repository = repository
        self._enqueuer = enqueuer
        self._clock = clock
        self._scan_limit = max(1, scan_limit)
        self._resolving_stale_after = max(0.0, resolving_stale_after_seconds)

    async def run_once(self, *, now: datetime | None = None) -> int:
        """Re-enqueue every currently-due row (and stale ``resolving`` orphan)
        that has no active release job.

        Returns the number of jobs newly enqueued (an already-chained row is a
        no-op). Fail-soft: a scan error never propagates into the reconcile loop."""
        resolved_now = self._resolve_now(now)
        try:
            due = await self._repository.list_due(
                now=resolved_now, limit=self._scan_limit,
            )
        except Exception:
            _LOGGER.exception("follow-up release reconcile: list_due failed")
            due = []
        try:
            stale = await self._repository.list_stale_resolving(
                now=resolved_now,
                older_than_seconds=self._resolving_stale_after,
                limit=self._scan_limit,
            )
        except Exception:
            _LOGGER.exception(
                "follow-up release reconcile: list_stale_resolving failed",
            )
            stale = []
        enqueued = 0
        seen: set[str] = set()
        for row in (*due, *stale):
            if row.id in seen:
                continue
            seen.add(row.id)
            if await self._enqueuer.enqueue(row, now=resolved_now):
                enqueued += 1
        if enqueued:
            _LOGGER.info(
                "follow-up release reconcile: re-enqueued %d row(s) "
                "(%d due, %d stale-resolving)",
                enqueued, len(due), len(stale),
            )
        return enqueued

    def _resolve_now(self, now: datetime | None) -> datetime:
        if now is not None:
            return ensure_utc(now)
        if self._clock is not None:
            return ensure_utc(self._clock.now())
        return datetime.now(timezone.utc)
