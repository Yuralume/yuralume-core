"""Event-driven release of pending follow-ups (HOSTED_CORE_SCALING §5 priority 1).

Phase 3 released busy-defer / scheduled-promise follow-ups by scanning
``list_due`` on *every* distributed social tick (a whole-table cursor walk). A
follow-up's release instant (``PendingFollowUp.scheduled_for``) is precisely known
the moment the row is written, so Phase 5 flips it to **event-driven**: the chat
write point enqueues one priority-1 one-shot job due at exactly that instant, and
the coordinator's low-frequency reconcile is only a safety net for missed events.

Three small collaborators live here — the whole feature is one cohesive unit:

PF3 adds a second release kind. A fulfilment may call a tool, and one of those
tools is a GPU; the release job itself is priority 1 and exempt from every
background ceiling, because what it owes is a message a player is waiting for.
Rather than choose between "an owed message queues behind a picture" and "a
picture renders outside every cap", the release **splits after the composer has
chosen**: the text half stays exempt, and the render is handed to
``pending_follow_up_image_release``, whose claim the §5 ``image`` ceiling counts.
The hand-off is a fact, not a prediction — nothing at enqueue time knows whether
a promise will want a photo, and guessing would leak the gate in one direction
and strand text fulfilments in the other.

* :class:`PendingFollowUpReleaseEnqueuer` — the single enqueue path, shared by the
  chat write points, the reconcile and (via ``defer_capability``) the PF3 hand-off.
  It reads the *current* coordinator epoch via
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
    PENDING_FOLLOW_UP_IMAGE_RELEASE_KIND,
    PENDING_FOLLOW_UP_RELEASE_KIND,
    JobCapability,
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

#: Which release kind serves a given provider capability (PF3). Only ``image`` has
#: a second kind: it is the one capability whose ceiling models a physical GPU, so
#: it is the one whose invocation cannot simply run wherever it was discovered. A
#: capability absent from this map is not deferrable — the caller runs it inline,
#: which is the correct answer for every tool that costs nothing scarce.
_CAPABILITY_RELEASE_KINDS: dict[str, str] = {
    JobCapability.IMAGE.value: PENDING_FOLLOW_UP_IMAGE_RELEASE_KIND,
}

_ENQUEUE_CREATED = "created"
_ENQUEUE_ACTIVE = "active"
"""A job for this row already exists — the queue's active-idempotency index
refused the insert. For a *deferral* that is a success, not a failure: the
work is already scheduled where it needs to run.

``enqueue`` reports this and a fencing rejection with the same ``None``, and
the two demand opposite answers (already-scheduled → stop; nothing inserted →
run it yourself), so :meth:`PendingFollowUpReleaseEnqueuer._classify_refusal`
separates them before this value is used."""
_ENQUEUE_UNAVAILABLE = "unavailable"
"""No live coordinator lease: embedded self-host, or a leaderless gap. Nothing
was scheduled and nothing will be — the caller must handle the work itself."""


def _release_window(scheduled_for: datetime) -> int:
    """Idempotency window derived from the row's release instant.

    ``scheduled_for`` is the row's own release instant, so both the write-point
    enqueue and the reconcile compute the SAME ``follow_up:{id}:{window}`` key from
    whatever the row currently says → the queue accepts at most one active release
    job per row. A later occurrence of the same row id (a failed release flipped
    back to ``queued`` after its job reached a terminal state) reuses the same key,
    which is accepted only because the previous job is no longer active.

    An honesty park (F1) is the one thing that *moves* ``scheduled_for``, and it
    moves it only forward, only after its own release job has already run to a
    terminal state. So the key changes, the next enqueue mints a job at the new
    instant, and any straggler minted under the old key is answered by the
    handler's ``not_due`` re-verify rather than by an early release."""
    return int(ensure_utc(scheduled_for).timestamp())


def _release_idempotency_key(row: PendingFollowUp, kind: str) -> str:
    """Per-(row, window, kind) key.

    The kind is part of the key on purpose: the image half is minted while the
    text half is still CLAIMED — i.e. still active — so a shared key would have
    the queue reject exactly the hand-off this design depends on."""
    base = f"follow_up:{row.id}:{_release_window(row.scheduled_for)}"
    return base if kind == PENDING_FOLLOW_UP_RELEASE_KIND else f"{base}:{kind}"


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
        outcome = await self._enqueue(
            row, kind=PENDING_FOLLOW_UP_RELEASE_KIND, now=self._resolve_now(now),
        )
        return outcome == _ENQUEUE_CREATED

    async def defer_capability(
        self,
        row: PendingFollowUp,
        *,
        capability: str,
        now: datetime | None = None,
    ) -> bool:
        """Take ownership of a fulfilment's ``capability`` half (PF3).

        Returns True when a job that will run it under that capability's ceiling
        exists — freshly minted, or already there from an earlier pass. The caller
        must then stop and send nothing this round.

        Returns False for "nobody can take this": an un-deferrable capability, or
        no live coordinator (embedded self-host, leaderless gap). The caller runs
        the tool inline — the same thing it did before this hand-off existed, which
        is what keeps the embedded path byte-identical and keeps the promise kept
        when there is no queue to hand it to."""
        kind = _CAPABILITY_RELEASE_KINDS.get(capability)
        if kind is None:
            return False
        outcome = await self._enqueue(
            row, kind=kind, now=self._resolve_now(now),
        )
        if outcome == _ENQUEUE_UNAVAILABLE:
            _LOGGER.info(
                "follow-up release: no distributed queue to take the %s half "
                "id=%s — running it inline", capability, row.id,
            )
            return False
        _LOGGER.info(
            "follow-up release: %s half %s id=%s kind=%s",
            capability, outcome, row.id, kind,
        )
        return True

    async def _enqueue(
        self, row: PendingFollowUp, *, kind: str, now: datetime,
    ) -> str:
        try:
            current = await self._lease.current(COORDINATOR_LEASE_NAME)
        except Exception:
            _LOGGER.exception(
                "follow-up release enqueue: lease read failed id=%s", row.id,
            )
            return _ENQUEUE_UNAVAILABLE
        if current is None:
            # No coordinator has ever held the lease → embedded / not distributed.
            return _ENQUEUE_UNAVAILABLE
        _owner, epoch, lease_until = current
        if ensure_utc(lease_until) < now:
            # The last leader's lease has expired; the enqueue would fence out.
            # Leave it — the next leader's reconcile re-enqueues the still-due row.
            return _ENQUEUE_UNAVAILABLE
        spec = BackgroundJobSpec(
            kind=kind,
            idempotency_key=_release_idempotency_key(row, kind),
            due_at=ensure_utc(row.scheduled_for),
            fencing_epoch=epoch,
            priority=_RELEASE_PRIORITY,
            character_id=row.character_id,
            # Payload carries the row id ONLY (no chat content — §3.1 / §11); the
            # handler re-loads the row and re-verifies it before releasing. That
            # red line is also why the image half re-composes instead of carrying
            # the pending tool call: a rendered prompt is derived chat content, and
            # the queue is not where content lives.
            payload={"follow_up_id": row.id},
        )
        try:
            job_id = await self._queue.enqueue(spec, now=now)
        except Exception:
            _LOGGER.exception(
                "follow-up release enqueue failed id=%s character=%s kind=%s",
                row.id, row.character_id, kind,
            )
            return _ENQUEUE_UNAVAILABLE
        if job_id is not None:
            return _ENQUEUE_CREATED
        return await self._classify_refusal(row, kind=kind, epoch=epoch, now=now)

    async def _classify_refusal(
        self, row: PendingFollowUp, *, kind: str, epoch: int, now: datetime,
    ) -> str:
        """Tell a duplicate apart from a fencing rejection after a ``None``.

        The port returns ``None`` for both "an active job already covers this"
        and "your coordinator epoch is stale / expired, nothing was inserted".
        Reading both as "already active" is how a *deferral* comes to report
        success for work that does not exist: the caller then sends nothing and
        parks the row, and the promise waits for the next reconcile instead of
        being kept right now.

        The lease's epoch is what separates them, because it is **monotonic**:
        it only ever advances, on an ownership change, and an expired lease can
        only come back with a higher one (``renew`` refuses an expired lease;
        re-``acquire`` bumps). So re-reading it *after* the enqueue is enough —
        an epoch still equal to the one we fenced under, still live at the same
        ``now``, means the queue's own check could not have failed at the
        (earlier) enqueue instant, leaving the idempotency index as the only
        explanation. Anything else is treated as "nothing was scheduled", which
        is the fail-soft direction: the caller runs the tool inline and the
        promise is kept."""
        try:
            current = await self._lease.current(COORDINATOR_LEASE_NAME)
        except Exception:
            _LOGGER.exception(
                "follow-up release enqueue: lease re-read failed id=%s", row.id,
            )
            return _ENQUEUE_UNAVAILABLE
        if current is not None:
            _owner, current_epoch, lease_until = current
            if current_epoch == epoch and ensure_utc(lease_until) >= now:
                return _ENQUEUE_ACTIVE
        _LOGGER.info(
            "follow-up release enqueue: refused under epoch %s id=%s kind=%s — "
            "the coordinator changed under us, so nothing was scheduled",
            epoch, row.id, kind,
        )
        return _ENQUEUE_UNAVAILABLE

    def _resolve_now(self, now: datetime | None) -> datetime:
        if now is not None:
            return ensure_utc(now)
        if self._clock is not None:
            return ensure_utc(self._clock.now())
        return datetime.now(timezone.utc)


def release_idempotency_keys(row: PendingFollowUp) -> tuple[str, ...]:
    """Every idempotency key a release job for ``row`` can carry.

    Derived from the row rather than looked up, exactly like the enqueue
    side — both compute the same ``follow_up:{id}:{window}`` base from
    ``scheduled_for``, which is fixed for the life of the row."""
    kinds = (
        PENDING_FOLLOW_UP_RELEASE_KIND,
        *sorted(_CAPABILITY_RELEASE_KINDS.values()),
    )
    return tuple(_release_idempotency_key(row, kind) for kind in kinds)


class PendingFollowUpReleaseWithdrawer:
    """Take back the release jobs of a row that no longer exists.

    The mirror image of :class:`PendingFollowUpReleaseEnqueuer`, and its
    only caller today is turn-undo: a row the reverted turn created gets
    deleted, so the job scheduled to release it now describes work that
    is gone. The handler re-loads its row and skips a missing one, so
    nothing breaks without this — what it buys is a queue whose contents
    still describe real work, and an active-idempotency key freed now
    instead of at the row's original due instant.

    Every release kind minted for the row is withdrawn, not only the text
    one: the image half is a second key under the same row, and leaving
    it holds a capability slot for a promise nobody is owed. No
    coordinator lease is read — withdrawal removes work rather than
    admitting it, so there is no epoch to fence against.
    """

    def __init__(self, *, queue: BackgroundJobQueuePort) -> None:
        self._queue = queue

    async def withdraw(self, row: PendingFollowUp, *, now: datetime) -> int:
        """Withdraw every queued release job for ``row``; count retired.

        Never raises: the caller is mid-rollback, and a queue hiccup must
        not cost the player the rest of the undo."""
        withdrawn = 0
        for key in release_idempotency_keys(row):
            try:
                withdrawn += await self._queue.withdraw_queued(
                    key, now=ensure_utc(now),
                )
            except Exception:
                _LOGGER.exception(
                    "follow-up release withdraw failed id=%s key=%s",
                    row.id, key,
                )
        return withdrawn


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
    reconcile re-enqueues it later — the job is one-shot, never a self-chain.

    One handler serves BOTH release kinds (PF3). They differ in exactly one thing —
    whether the claim that produced this job holds a capability slot — and that is
    read off ``job.kind``, so the text and image halves can never drift into two
    copies of the same re-verify / delegate logic."""

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
        # PF3: the job's own kind says whether it holds a capability slot. The base
        # release does not, so it hands a GPU-bound fulfilment over to the image
        # kind; a job OF that image kind was claimed under the ceiling and runs the
        # invocation itself. Deferring again there would be an infinite hand-off.
        released = await self._dispatcher.release_row(
            row,
            now=resolved_now,
            defer_capabilities=job.kind not in _CAPABILITY_RELEASE_KINDS.values(),
        )
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
    re-sending an already-delivered message.

    A row parked waiting for an image slot (PF3) is ``queued`` and still due, so it
    is swept like any other: each sweep re-runs its first compose pass and re-lands
    on the already-active image job (a no-op). That replay is deliberate rather than
    suppressed — it costs one compose per 15 minutes, and it is also what re-mints
    the image job if that one died, so the promise cannot be stranded by losing a
    single queue row."""

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
