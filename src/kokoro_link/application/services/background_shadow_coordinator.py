"""Shadow coordinator.

The coordinator is the *left half* of the Phase 2 shadow: it maintains the
single-leader ``background-coordinator`` lease and, once per bucket, enqueues one
global ``social_tick`` job. Nothing here runs providers or mutates domain state;
it only *populates the queue* so the worker (§3.3) executes the distributed work.

**Phase 5 §13 cutover** (distributed path): the coordinator NO LONGER enqueues a
per-character ``character_tick`` every bucket — that whole-table scan is replaced
by per-kind self-continuing chains, which the low-frequency
:class:`DueJobReconciler` (~15 min, :meth:`_maybe_reconcile`) reseeds when a
character's chain is missing (new / thawed / broken). Only the global
``social_tick`` remains per-bucket here (cross-character work is not yet
per-kind split). The embedded+shadow **mirror** path (``mirror_from_journal``,
:meth:`_enqueue_from_journal`) is UNCHANGED — the soak still mirrors the embedded
journal bucket-for-bucket and runs NO reconcile.

Design invariants:

* **Leader lease** — acquire the lease (TTL 30s), renew every loop. A lost
  renewal drops the coordinator to passive (no enqueue) and it keeps trying to
  re-acquire; a takeover bumps the fencing epoch, and every spec it enqueues
  carries the *currently held* epoch so a partitioned ex-leader is fenced out
  by the queue.
* **Bucket idempotency** — work is bucketed by ``floor(unix_now /
  bucket_seconds)``. A durable cursor records the last enqueued bucket so a
  restart mid-day does not re-enqueue an already-covered bucket, and the
  queue's active-idempotency index makes a same-bucket re-run a cheap no-op
  (counted as ``deduped``).
* **Fail-soft** — one bad iteration logs and the loop keeps running; the
  coordinator must never die on a transient DB error.
* **Retention** — once an hour the loop invokes ``queue.prune`` + ``journal
  .prune`` (the 7d/30d policy itself lives in the P2-A adapters).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from kokoro_link.contracts.background_jobs import (
    COORDINATOR_LEASE_NAME,
    BackgroundCoordinatorCursorPort,
    BackgroundCoordinatorLeasePort,
    BackgroundJobQueuePort,
    BackgroundJobSpec,
    TickJournalPort,
)
from kokoro_link.contracts.clock import ClockPort, ensure_utc
from kokoro_link.contracts.execution_mode import MODE_DISTRIBUTED, RuntimeOwnershipPort
from kokoro_link.contracts.operator_profile import OperatorProfileRepositoryPort
from kokoro_link.contracts.repositories import CharacterRepositoryPort

if TYPE_CHECKING:
    from kokoro_link.application.services.due_job_reconciler import DueJobReconciler

_LOGGER = logging.getLogger(__name__)

_DEFAULT_LOOP_SECONDS = 15.0
_DEFAULT_BUCKET_SECONDS = 300
_LEASE_TTL_SECONDS = 30
_PRUNE_INTERVAL_SECONDS = 3600.0
_LEASE_RENEW_INTERVAL_SECONDS = _LEASE_TTL_SECONDS / 3
# Phase 5 §4.2: the reconcile is a low-frequency reseed pass (~15 min) that
# relinks a character's missing per-kind chain (new / thawed / broken). It is
# NOT a per-bucket scan — the self-chains carry the steady-state cadence.
_DEFAULT_RECONCILE_INTERVAL_SECONDS = 900.0

CHARACTER_TICK_KIND = "character_tick"
SOCIAL_TICK_KIND = "social_tick"
# Enqueued out-of-band by CharacterService on create (P3-C durable warmup), NOT
# by the coordinator's per-bucket enqueue; lives here so worker + service + wiring
# share one kind string.
CHARACTER_WARMUP_KIND = "character_warmup"
_CHARACTER_TICK_PRIORITY = 3
_SOCIAL_TICK_PRIORITY = 4
_ENQUEUE_CURSOR_NAME = "shadow-enqueue"


@dataclass(slots=True)
class ShadowCounters:
    """In-memory observability counters shared by both shadow services.

    A single flat shape keeps the metrics renderer and the diagnostics route
    uniform. Each service increments only the fields it is responsible for
    (the coordinator: ``enqueued`` / ``deduped`` / ``lease_lost``; the worker:
    ``claimed`` / ``completed`` / ``skipped`` / ``failed`` / ``lease_lost``),
    so the two snapshots sum cleanly with no double counting.
    """

    enqueued: int = 0
    deduped: int = 0
    blocked_missing_signal: int = 0
    journal_skipped_missing_character: int = 0
    claimed: int = 0
    completed: int = 0
    skipped: int = 0
    failed: int = 0
    lease_lost: int = 0

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


def bucket_for(now: datetime, bucket_seconds: int) -> int:
    """``floor(unix_seconds / bucket_seconds)`` — the shared bucketing used by
    the coordinator's enqueue and the scheduler's embedded journal so the two
    sides of the §14 comparison align exactly."""
    return int(ensure_utc(now).timestamp()) // bucket_seconds


def bucket_start(bucket: int, bucket_seconds: int) -> datetime:
    """Tz-aware UTC start instant of ``bucket`` (used as the spec ``due_at``)."""
    return datetime.fromtimestamp(bucket * bucket_seconds, tz=timezone.utc)


class ShadowCoordinator:
    """Leader-elected shadow enqueuer. Same start/stop/liveness surface as the
    schedulers so ``/health`` can cover it."""

    def __init__(
        self,
        *,
        queue: BackgroundJobQueuePort,
        lease: BackgroundCoordinatorLeasePort,
        cursor: BackgroundCoordinatorCursorPort,
        journal: TickJournalPort,
        character_repository: CharacterRepositoryPort,
        owner_id: str,
        operator_profile_repository: OperatorProfileRepositoryPort | None = None,
        loop_seconds: float = _DEFAULT_LOOP_SECONDS,
        bucket_seconds: int = _DEFAULT_BUCKET_SECONDS,
        clock: ClockPort | None = None,
        runtime_ownership: RuntimeOwnershipPort | None = None,
        mirror_from_journal: bool = False,
        due_job_reconciler: "DueJobReconciler | None" = None,
        reconcile_interval_seconds: float = _DEFAULT_RECONCILE_INTERVAL_SECONDS,
    ) -> None:
        self._queue = queue
        self._lease = lease
        self._cursor = cursor
        self._journal = journal
        self._characters = character_repository
        # Optional operator→tenant resolver (H3 identity chain). When wired,
        # each enqueued spec carries the character's authoritative operator
        # (``character.user_id``) AND the operator's Cloud tenant
        # (``operator.cloud_tenant_id``); ``None`` in a non-cloud/local soak, so
        # the tenant axis is only truly exercised in a cloud-mode soak.
        self._operator_profiles = operator_profile_repository
        self._owner_id = owner_id
        self._loop_seconds = loop_seconds
        self._bucket_seconds = bucket_seconds
        self._clock = clock
        self._runtime_ownership = runtime_ownership
        self._mirror_from_journal = mirror_from_journal
        # Phase 5 §4.2: the per-kind chain reseeder. Shares the coordinator's
        # leader epoch (its ``epoch_provider`` reads ``coordinator.epoch``) so it
        # never holds a second lease. ``None`` on the embedded+shadow mirror path
        # and in a pre-Phase-5 wiring — the coordinator then reseeds nothing.
        self._reconciler = due_job_reconciler
        self._reconcile_interval_seconds = reconcile_interval_seconds
        self._last_reconcile_at: datetime | None = None
        self._counters = ShadowCounters()
        # ``None`` while passive; the currently-held fencing epoch while leader.
        self._epoch: int | None = None
        self._last_prune_at: datetime | None = None
        self._task: asyncio.Task | None = None
        self._stop_event: asyncio.Event | None = None

    # -- liveness surface (mirrors ProactiveScheduler) --------------------- #

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(
            self._run(), name="shadow-coordinator",
        )

    async def stop(self) -> None:
        if self._task is None or self._stop_event is None:
            return
        self._stop_event.set()
        try:
            await asyncio.wait_for(self._task, timeout=5.0)
        except asyncio.TimeoutError:
            self._task.cancel()
        finally:
            self._task = None
            self._stop_event = None
            self._epoch = None

    @property
    def started(self) -> bool:
        return self._task is not None

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def set_runtime_ownership(self, ownership: RuntimeOwnershipPort | None) -> None:
        self._runtime_ownership = ownership

    def set_due_job_reconciler(
        self,
        reconciler: "DueJobReconciler | None",
        *,
        interval_seconds: float | None = None,
    ) -> None:
        """Wire the Phase 5 per-kind chain reseeder after construction.

        The reconciler depends on the runtime profile resolver / executors that
        are built later in the container than the coordinator, so it is injected
        here (mirrors :meth:`set_runtime_ownership`). ``interval_seconds``
        optionally overrides the reconcile cadence (env-driven)."""
        self._reconciler = reconciler
        if interval_seconds is not None:
            self._reconcile_interval_seconds = interval_seconds

    @property
    def epoch(self) -> int | None:
        """Currently-held fencing epoch, or ``None`` while passive."""
        return self._epoch

    def snapshot(self) -> ShadowCounters:
        return ShadowCounters(**self._counters.as_dict())

    async def owns_live_lease(self, *, now: datetime | None = None) -> bool:
        """Confirm this incarnation still owns the durable coordinator lease."""
        resolved = self._resolve_now(now)
        try:
            current = await self._lease.current(COORDINATOR_LEASE_NAME)
        except Exception:
            _LOGGER.exception(
                "shadow coordinator: live lease verification failed; "
                "failing closed",
            )
            return False
        if current is None or self._epoch is None:
            return False
        owner_id, epoch, lease_until = current
        return (
            owner_id == self._owner_id
            and epoch == self._epoch
            and ensure_utc(lease_until) >= resolved
        )

    # -- loop -------------------------------------------------------------- #

    async def _run(self) -> None:
        assert self._stop_event is not None
        _LOGGER.info(
            "shadow coordinator started owner=%s bucket=%ss loop=%.0fs",
            self._owner_id, self._bucket_seconds, self._loop_seconds,
        )
        try:
            await self.run_once()
        except Exception:
            _LOGGER.exception("shadow coordinator: initial iteration failed")
        try:
            while not self._stop_event.is_set():
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(), timeout=self._loop_seconds,
                    )
                except asyncio.TimeoutError:
                    pass
                if self._stop_event.is_set():
                    break
                try:
                    await self.run_once()
                except Exception:
                    _LOGGER.exception("shadow coordinator: iteration failed")
        except asyncio.CancelledError:
            pass
        _LOGGER.info("shadow coordinator stopped owner=%s", self._owner_id)

    async def run_once(self, *, now: datetime | None = None) -> None:
        """One coordinator pass: maintain the lease, and while leader enqueue
        the current bucket + run the hourly retention prune. Deterministic and
        side-effect-scoped so tests can drive it directly with a fake clock."""
        resolved = self._resolve_now(now)
        if self._runtime_ownership is not None:
            try:
                mode, _ = await self._runtime_ownership.get()
            except Exception:
                _LOGGER.exception("shadow coordinator: ownership read failed")
                return
            if mode != MODE_DISTRIBUTED:
                return
        await self._maintain_lease(resolved)
        if self._epoch is None:
            return
        if self._mirror_from_journal:
            # Embedded+shadow soak path — UNCHANGED (§ red line). No per-kind
            # reconcile here: the embedded scheduler still owns all work; this
            # branch only mirrors the journal into the queue for §14 comparison.
            await self._enqueue_from_journal(self._epoch, resolved)
        else:
            # Phase 5 §13 cutover COMPLETE: the coordinator no longer enqueues the
            # global ``social_tick`` per bucket. The last whole-table sweep — the
            # cross-character encounter / peer-knowledge / persona-dream work — is
            # now per-target self-continuing chains (``encounter_tick`` per pair,
            # ``persona_dream`` / ``peer_knowledge`` per character), reseeded by the
            # low-frequency reconcile. The ``social_tick`` handler stays wired for
            # redrive of any legacy queued row (§ compat).
            await self._maybe_reconcile(resolved)
        await self._maybe_prune(resolved)

    async def _maintain_lease(self, now: datetime) -> None:
        if self._epoch is None:
            self._epoch = await self._lease.acquire(
                COORDINATOR_LEASE_NAME,
                self._owner_id,
                ttl_seconds=_LEASE_TTL_SECONDS,
                now=now,
            )
            return
        renewed = await self._lease.renew(
            COORDINATOR_LEASE_NAME,
            self._owner_id,
            ttl_seconds=_LEASE_TTL_SECONDS,
            now=now,
        )
        if renewed is None:
            # Lost ownership (partition / takeover). Drop to passive and try to
            # re-acquire immediately; a successful re-acquire bumps the epoch,
            # so any spec we enqueue afterwards carries the NEW epoch.
            self._counters.lease_lost += 1
            _LOGGER.warning(
                "shadow coordinator lease lost owner=%s; dropping to passive",
                self._owner_id,
            )
            self._epoch = await self._lease.acquire(
                COORDINATOR_LEASE_NAME,
                self._owner_id,
                ttl_seconds=_LEASE_TTL_SECONDS,
                now=now,
            )
        else:
            self._epoch = renewed

    async def _enqueue_from_journal(self, epoch: int, now: datetime) -> None:
        """Mirror the earliest journal bucket visible after the cursor.

        The social row is the journal's commit marker.  We deliberately discover
        only one bucket and fetch only that bucket: a partial earliest bucket must
        block rather than allowing a later complete bucket to leapfrog it.
        """
        cursor = await self._cursor.get(_ENQUEUE_CURSOR_NAME)
        last_bucket = cursor.get("last_bucket") if cursor else None
        if not isinstance(last_bucket, int):
            last_bucket = -1
        current_bucket = bucket_for(now, self._bucket_seconds)
        if last_bucket >= current_bucket:
            return
        try:
            earliest = await self._journal.earliest_bucket_after(
                after_bucket=last_bucket,
                to_bucket=current_bucket,
            )
            if earliest is None:
                self._counters.blocked_missing_signal += 1
                return
            rows = await self._journal.list_bucket(earliest)
        except Exception:
            _LOGGER.exception("shadow coordinator: journal read failed; failing closed")
            return

        if not any(
            kind == SOCIAL_TICK_KIND and character_id is None
            for _bucket, kind, character_id, _recorded_at in rows
        ):
            self._counters.blocked_missing_signal += 1
            return

        # Journal adapters are expected to deduplicate, but retain this set here
        # so a future adapter or a replayed row cannot enqueue duplicate work.
        entries: set[tuple[str, str | None]] = {
            (kind, character_id)
            for _bucket, kind, character_id, _recorded_at in rows
            if (
                kind == CHARACTER_TICK_KIND and character_id is not None
            ) or (
                kind == SOCIAL_TICK_KIND and character_id is None
            )
        }
        due_at = bucket_start(earliest, self._bucket_seconds)
        tenant_cache: dict[str, str | None] = {}
        last_checked = now
        for kind, character_id in sorted(
            entries, key=lambda item: (item[0], item[1] or ""),
        ):
            if kind == SOCIAL_TICK_KIND:
                spec = BackgroundJobSpec(
                    kind=SOCIAL_TICK_KIND,
                    idempotency_key=f"{SOCIAL_TICK_KIND}:{earliest}",
                    due_at=due_at,
                    fencing_epoch=epoch,
                    priority=_SOCIAL_TICK_PRIORITY,
                    payload={"bucket": earliest},
                )
            else:
                character = await self._characters.get(character_id)
                if character is None:
                    # Keep the journal's identity observable in the queue.  The
                    # worker will reload it and produce the valid
                    # ``missing_character`` skip outcome.
                    self._counters.journal_skipped_missing_character += 1
                    operator_id = None
                    tenant_id = None
                else:
                    operator_id = character.user_id or None
                    tenant_id = await self._resolve_tenant(
                        operator_id, tenant_cache,
                    )
                spec = BackgroundJobSpec(
                    kind=CHARACTER_TICK_KIND,
                    idempotency_key=(
                        f"{CHARACTER_TICK_KIND}:{character_id}:{earliest}"
                    ),
                    due_at=due_at,
                    fencing_epoch=epoch,
                    priority=_CHARACTER_TICK_PRIORITY,
                    tenant_id=tenant_id,
                    operator_id=operator_id,
                    character_id=character_id,
                    payload={
                        "character_id": character_id,
                        "bucket": earliest,
                    },
                )
            last_checked, accepted = await self._enqueue_one(
                spec, epoch, last_checked,
            )
            if not accepted:
                return

        if self._epoch != epoch:
            return
        await self._advance_cursor(epoch, earliest)

    async def _enqueue_one(
        self,
        spec: BackgroundJobSpec,
        epoch: int,
        last_checked: datetime,
    ) -> tuple[datetime, bool]:
        """Enqueue with a fresh timestamp and bounded lease heartbeats."""
        fresh = self._fresh_now()
        if (
            fresh - last_checked
        ).total_seconds() >= _LEASE_RENEW_INTERVAL_SECONDS:
            renewed = await self._lease.renew(
                COORDINATOR_LEASE_NAME,
                self._owner_id,
                ttl_seconds=_LEASE_TTL_SECONDS,
                now=fresh,
            )
            if renewed != epoch:
                self._mark_lease_lost()
                return fresh, False
            self._epoch = renewed
            last_checked = fresh
        job_id = await self._queue.enqueue(spec, now=fresh)
        # Verify the lease after the adapter call as well as before it.  A
        # durable adapter may spend enough time in the transaction for the
        # lease to expire (or for a takeover to commit) after the pre-call
        # heartbeat.  Do not continue a partially fenced batch in that case,
        # even when the adapter returned a job id rather than its ambiguous
        # ``None`` sentinel.
        observed = self._fresh_now()
        if not await self._still_leader(epoch, observed):
            self._mark_lease_lost()
            return observed, False
        self._record_enqueue(job_id)
        return observed, True

    async def _advance_cursor(self, epoch: int, bucket: int) -> None:
        fresh = self._fresh_now()
        advanced = await self._cursor.advance_if_leader(
            _ENQUEUE_CURSOR_NAME,
            bucket,
            COORDINATOR_LEASE_NAME,
            self._owner_id,
            epoch,
            fresh,
        )
        if not advanced:
            self._mark_lease_lost()

    def _mark_lease_lost(self) -> None:
        self._counters.lease_lost += 1
        self._epoch = None

    async def _resolve_tenant(
        self, operator_id: str | None, cache: dict[str, str | None],
    ) -> str | None:
        """Resolve the operator's Cloud tenant id, cached per enqueue batch.

        ``None`` when no operator resolver is wired (local soak), the operator
        is unknown, or the operator carries no Cloud tenant — all legitimate
        non-cloud states, so the worker's tenant check is a no-op there."""
        if self._operator_profiles is None or operator_id is None:
            return None
        if operator_id in cache:
            return cache[operator_id]
        operator = await self._operator_profiles.get(operator_id)
        tenant = operator.cloud_tenant_id if operator is not None else None
        cache[operator_id] = tenant
        return tenant

    async def _still_leader(self, epoch: int, now: datetime) -> bool:
        current = await self._lease.current(COORDINATOR_LEASE_NAME)
        return (
            current is not None
            and current[0] == self._owner_id
            and current[1] == epoch
            and ensure_utc(current[2]) >= now
        )

    def _record_enqueue(self, job_id: str | None) -> None:
        # ``None`` = already queued for this bucket or stale epoch — both are
        # the idempotent path, counted as deduped rather than an error.
        if job_id is None:
            self._counters.deduped += 1
        else:
            self._counters.enqueued += 1

    async def _maybe_reconcile(self, now: datetime) -> None:
        """Run the per-kind chain reseeder on its own low-frequency cadence.

        Distributed (non-mirror) path only. The first pass runs immediately on
        startup so a fresh leader relinks every active character's chains; then
        every ``reconcile_interval_seconds`` (~15 min). Fail-soft: a reconcile
        error never kills the coordinator loop. The reconcile is a no-op when the
        reconciler is unwired or when the coordinator is not the leader (its
        ``epoch_provider`` reads ``self.epoch`` — ``None`` while passive)."""
        if self._reconciler is None:
            return
        if (
            self._last_reconcile_at is not None
            and (now - self._last_reconcile_at).total_seconds()
            < self._reconcile_interval_seconds
        ):
            return
        self._last_reconcile_at = now
        try:
            await self._reconciler.run_once(now=now)
        except Exception:
            _LOGGER.exception("shadow coordinator: due-job reconcile failed")

    async def _maybe_prune(self, now: datetime) -> None:
        if (
            self._last_prune_at is not None
            and (now - self._last_prune_at).total_seconds() < _PRUNE_INTERVAL_SECONDS
        ):
            return
        self._last_prune_at = now
        try:
            await self._queue.prune(now=now)
            await self._journal.prune(now=now)
        except Exception:
            _LOGGER.exception("shadow coordinator: retention prune failed")

    def _fresh_now(self) -> datetime:
        return self._resolve_now(None)

    def _resolve_now(self, now: datetime | None) -> datetime:
        if now is not None:
            return ensure_utc(now)
        if self._clock is not None:
            return ensure_utc(self._clock.now())
        return datetime.now(timezone.utc)
