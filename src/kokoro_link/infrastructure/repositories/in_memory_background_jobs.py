"""In-memory background-queue adapters — first-class test citizens.

Full-fidelity implementations of the P2-A ports: same idempotency,
lease-expiry, epoch-monotonicity and retention semantics as the
SQLAlchemy adapters, so the unit suite exercises real queue mechanics
without a database. See ``contracts/background_jobs.py``.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping
from uuid import uuid4

from kokoro_link.contracts.background_jobs import (
    ACTIVE_STATUSES,
    COORDINATOR_LEASE_NAME,
    BackgroundCoordinatorCursorPort,
    BackgroundCoordinatorLeasePort,
    BackgroundJob,
    BackgroundJobSpec,
    BackgroundJobQueuePort,
    ClaimedJob,
    JobOutcome,
    JobStatus,
    QueueStats,
    TickJournalPort,
    summarize_error,
)
from kokoro_link.contracts.clock import ensure_utc
from kokoro_link.contracts.due_jobs import capability_kind_sets
from kokoro_link.contracts.execution_mode import (
    ABSENT_EPOCH,
    ABSENT_MODE,
    RuntimeOwnershipPort,
    VALID_MODES,
)


_LOGGER = logging.getLogger(__name__)

_TERMINAL_SHORT = frozenset(
    {JobStatus.DONE, JobStatus.FAILED, JobStatus.SUPERSEDED},
)
_SHORT_RETENTION = timedelta(days=7)
_DEAD_RETENTION = timedelta(days=30)
_EPOCH_ZERO = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class _JobRecord:
    """Mutable in-memory row (converted to frozen views on read)."""

    id: str
    kind: str
    status: JobStatus
    idempotency_key: str
    due_at: datetime
    priority: int
    fencing_epoch: int
    attempt_count: int
    max_attempts: int
    payload_version: int
    payload: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    tenant_id: str | None = None
    operator_id: str | None = None
    character_id: str | None = None
    lease_owner: str | None = None
    lease_until: datetime | None = None
    last_error: str | None = None
    outcome: dict[str, Any] | None = None
    claimed_at: datetime | None = None
    finished_at: datetime | None = None


class InMemoryBackgroundJobQueue(BackgroundJobQueuePort):
    """In-process durable-queue stand-in.

    ``lease`` couples the queue to a coordinator lease so ``enqueue`` can
    fence against the current epoch exactly like the SQLAlchemy adapter.
    When ``lease is None`` fencing is a no-op — used by unit tests that
    exercise queue mechanics (claim / retry / prune) in isolation.
    """

    def __init__(
        self,
        *,
        lease: BackgroundCoordinatorLeasePort | None = None,
    ) -> None:
        self._records: dict[str, _JobRecord] = {}
        self._lease = lease
        self._mutex = asyncio.Lock()

    async def enqueue(
        self, spec: BackgroundJobSpec, *, now: datetime,
    ) -> str | None:
        now = ensure_utc(now)
        async with self._mutex:
            if not await self._fencing_ok(spec, now):
                _LOGGER.warning(
                    "background enqueue rejected: stale/expired fencing epoch "
                    "kind=%s epoch=%s", spec.kind, spec.fencing_epoch,
                )
                return None
            for rec in self._records.values():
                if (
                    rec.idempotency_key == spec.idempotency_key
                    and rec.status in ACTIVE_STATUSES
                ):
                    return None
            job_id = uuid4().hex
            self._records[job_id] = _JobRecord(
                id=job_id,
                kind=spec.kind,
                status=JobStatus.QUEUED,
                idempotency_key=spec.idempotency_key,
                due_at=ensure_utc(spec.due_at),
                priority=spec.priority,
                fencing_epoch=spec.fencing_epoch,
                attempt_count=0,
                max_attempts=spec.max_attempts,
                payload_version=spec.payload_version,
                payload=dict(spec.payload),
                created_at=now,
                updated_at=now,
                tenant_id=spec.tenant_id,
                operator_id=spec.operator_id,
                character_id=spec.character_id,
            )
            return job_id

    async def _fencing_ok(self, spec: BackgroundJobSpec, now: datetime) -> bool:
        # Parity with the SA adapter's atomic fencing: the coordinator lease
        # must be present, still live at ``now`` and carry exactly the spec's
        # epoch. ``lease is None`` means fencing is disabled (unit tests that
        # exercise pure queue mechanics without a coordinator).
        if self._lease is None:
            return True
        current = await self._lease.current(COORDINATOR_LEASE_NAME)
        if current is None:
            return False
        _owner, epoch, lease_until = current
        return epoch == spec.fencing_epoch and ensure_utc(lease_until) >= now

    async def claim(
        self,
        worker_id: str,
        *,
        now: datetime,
        limit: int,
        lease_seconds: int,
        capability_caps: Mapping[str, int] | None = None,
    ) -> list[ClaimedJob]:
        now = ensure_utc(now)
        lease_until = now + timedelta(seconds=lease_seconds)
        # §5 capability caps: (kinds, cap) per capped capability. Parity with the
        # SA adapter's committed-count admission — the same posture as busy_tenants.
        capped = capability_kind_sets(dict(capability_caps)) if capability_caps else ()
        async with self._mutex:
            cap_inflight: dict[str, int] = {}
            for capability, _cap, kinds in capped:
                cap_inflight[capability] = sum(
                    1 for r in self._records.values()
                    if r.kind in kinds
                    and r.status == JobStatus.CLAIMED
                    and r.lease_until is not None
                    and r.lease_until >= now
                )
            cap_selected: dict[str, int] = {cap: 0 for cap, _c, _k in capped}
            candidates = [
                rec for rec in self._records.values()
                if self._is_claimable(rec, now)
            ]
            candidates.sort(key=lambda r: (r.priority, r.due_at))
            # Per-tenant fairness (P3-C §5): a tenant that already has a LIVE
            # claimed job elsewhere (another worker still within lease) is capped
            # — skip its ready jobs this batch — and within this batch we keep at
            # most one job per tenant (approximate fairness; the exact cluster-
            # wide cap is deferred, bounded by job-level idempotency + slots).
            # ``tenant_id is None`` jobs (social_tick, local soak) are exempt.
            busy_tenants = {
                r.tenant_id for r in self._records.values()
                if r.tenant_id is not None
                and r.status == JobStatus.CLAIMED
                and r.lease_until is not None
                and r.lease_until >= now
            }
            batch_tenants: set[str] = set()
            claimed: list[ClaimedJob] = []
            for rec in candidates:
                if len(claimed) >= limit:
                    break
                reclaim = rec.status == JobStatus.CLAIMED
                if reclaim and rec.attempt_count >= rec.max_attempts:
                    # A repeatedly-crashing job whose lease keeps expiring
                    # must not be re-claimed forever — retire it to dead.
                    rec.status = JobStatus.DEAD
                    rec.last_error = rec.last_error or summarize_error(
                        "lease expired: max attempts exhausted",
                    )
                    rec.lease_owner = None
                    rec.lease_until = None
                    rec.finished_at = now
                    rec.updated_at = now
                    continue
                if rec.tenant_id is not None and (
                    rec.tenant_id in busy_tenants
                    or rec.tenant_id in batch_tenants
                ):
                    # Tenant already at its per-batch cap — leave unclaimed.
                    continue
                # §5 capability cap: skip a job whose provider capability is
                # already at its global in-flight ceiling (committed live-claimed
                # + selected earlier in this batch).
                capped_hit = False
                for capability, cap, kinds in capped:
                    if rec.kind in kinds and (
                        cap_inflight[capability] + cap_selected[capability] >= cap
                    ):
                        capped_hit = True
                        break
                if capped_hit:
                    continue
                rec.status = JobStatus.CLAIMED
                rec.attempt_count += 1
                for capability, _cap, kinds in capped:
                    if rec.kind in kinds:
                        cap_selected[capability] += 1
                if rec.tenant_id is not None:
                    batch_tenants.add(rec.tenant_id)
                rec.lease_owner = worker_id
                rec.lease_until = lease_until
                rec.claimed_at = now
                rec.updated_at = now
                claimed.append(_to_claimed(rec))
            return claimed

    @staticmethod
    def _is_claimable(rec: _JobRecord, now: datetime) -> bool:
        if rec.status == JobStatus.QUEUED:
            return rec.due_at <= now
        if rec.status == JobStatus.CLAIMED:
            return rec.lease_until is not None and rec.lease_until <= now
        return False

    async def extend_lease(
        self, job_id: str, worker_id: str, *, until: datetime, now: datetime,
    ) -> bool:
        now = ensure_utc(now)
        async with self._mutex:
            rec = self._records.get(job_id)
            if rec is None or rec.status != JobStatus.CLAIMED:
                return False
            if rec.lease_owner != worker_id:
                return False
            # Parity with complete/fail: an already-expired lease is not
            # extendable by its former holder — it must be re-claimed.
            if rec.lease_until is None or rec.lease_until < now:
                return False
            rec.lease_until = ensure_utc(until)
            rec.updated_at = now
            return True

    async def complete(
        self,
        job_id: str,
        worker_id: str,
        *,
        outcome: JobOutcome,
        now: datetime,
    ) -> bool:
        now = ensure_utc(now)
        async with self._mutex:
            rec = self._records.get(job_id)
            if not self._holds_valid_lease(rec, worker_id, now):
                return False
            assert rec is not None
            rec.status = JobStatus.DONE
            rec.outcome = dict(outcome.detail)
            rec.finished_at = now
            rec.updated_at = now
            rec.lease_until = None
            return True

    async def release_claim(
        self, job_id: str, worker_id: str, *, now: datetime,
    ) -> bool:
        now = ensure_utc(now)
        async with self._mutex:
            rec = self._records.get(job_id)
            if not self._holds_valid_lease(rec, worker_id, now):
                return False
            assert rec is not None
            rec.status = JobStatus.QUEUED
            # The claim charged an attempt but nothing ran — refund it so a
            # graceful stop never pushes a job closer to ``dead``. ``due_at``
            # is left alone: the job is ready right now for the next worker.
            rec.attempt_count = max(0, rec.attempt_count - 1)
            rec.lease_owner = None
            rec.lease_until = None
            rec.claimed_at = None
            rec.updated_at = now
            return True

    async def withdraw_queued(
        self, idempotency_key: str, *, now: datetime,
    ) -> int:
        now = ensure_utc(now)
        async with self._mutex:
            withdrawn = 0
            for rec in self._records.values():
                if (
                    rec.idempotency_key != idempotency_key
                    or rec.status != JobStatus.QUEUED
                ):
                    continue
                # Parity with the SA adapter: ``claimed`` rows are skipped
                # (their lease owner is the only party that may end them),
                # and the retired row goes to the terminal ``superseded``
                # marker so retention prunes it on the normal 7-day clock.
                rec.status = JobStatus.SUPERSEDED
                rec.lease_owner = None
                rec.lease_until = None
                rec.finished_at = now
                rec.updated_at = now
                withdrawn += 1
            return withdrawn

    async def fail(
        self,
        job_id: str,
        worker_id: str,
        *,
        error: BaseException | str,
        now: datetime,
        retry_in_seconds: int | None = None,
    ) -> bool:
        now = ensure_utc(now)
        async with self._mutex:
            rec = self._records.get(job_id)
            if not self._holds_valid_lease(rec, worker_id, now):
                return False
            assert rec is not None
            rec.last_error = summarize_error(error)
            rec.updated_at = now
            rec.lease_owner = None
            rec.lease_until = None
            if rec.attempt_count >= rec.max_attempts:
                rec.status = JobStatus.DEAD
                rec.finished_at = now
            elif retry_in_seconds is not None:
                rec.status = JobStatus.QUEUED
                rec.due_at = now + timedelta(seconds=retry_in_seconds)
                rec.claimed_at = None
            else:
                rec.status = JobStatus.FAILED
                rec.finished_at = now
            return True

    @staticmethod
    def _holds_valid_lease(
        rec: _JobRecord | None, worker_id: str, now: datetime,
    ) -> bool:
        return (
            rec is not None
            and rec.status == JobStatus.CLAIMED
            and rec.lease_owner == worker_id
            and rec.lease_until is not None
            and rec.lease_until >= now
        )

    async def stats(self, *, now: datetime) -> QueueStats:
        now = ensure_utc(now)
        status_counts: dict[str, int] = {}
        kind_counts: dict[str, int] = {}
        oldest_due: datetime | None = None
        async with self._mutex:
            for rec in self._records.values():
                status_counts[rec.status.value] = (
                    status_counts.get(rec.status.value, 0) + 1
                )
                kind_counts[rec.kind] = kind_counts.get(rec.kind, 0) + 1
                if rec.status == JobStatus.QUEUED and rec.due_at <= now:
                    if oldest_due is None or rec.due_at < oldest_due:
                        oldest_due = rec.due_at
            total = len(self._records)
        oldest_age = (
            (now - oldest_due).total_seconds() if oldest_due is not None else 0.0
        )
        return QueueStats(
            status_counts=status_counts,
            kind_counts=kind_counts,
            oldest_queued_age_seconds=oldest_age,
            total=total,
        )

    async def list_dead(self, *, limit: int) -> list[BackgroundJob]:
        async with self._mutex:
            dead = [
                rec for rec in self._records.values()
                if rec.status == JobStatus.DEAD
            ]
        dead.sort(
            key=lambda r: r.finished_at or r.updated_at, reverse=True,
        )
        return [_to_job(rec) for rec in dead[:limit]]

    async def redrive(self, job_id: str) -> bool:
        async with self._mutex:
            rec = self._records.get(job_id)
            if rec is None or rec.status != JobStatus.DEAD:
                return False
            # Parity with the SA active-idem partial index: another active row
            # may already hold this idempotency_key (the logical key recurred
            # while this one was dead). Reviving here would create a second
            # active row for the key — refuse, as the DB would.
            for other in self._records.values():
                if (
                    other.id != job_id
                    and other.idempotency_key == rec.idempotency_key
                    and other.status in ACTIVE_STATUSES
                ):
                    return False
            rec.status = JobStatus.QUEUED
            rec.attempt_count = 0
            rec.lease_owner = None
            rec.lease_until = None
            rec.last_error = None
            rec.finished_at = None
            rec.updated_at = _utcnow()
            return True

    async def prune(self, *, now: datetime) -> int:
        now = ensure_utc(now)
        removed = 0
        async with self._mutex:
            for job_id in list(self._records.keys()):
                rec = self._records[job_id]
                anchor = rec.finished_at or rec.updated_at
                if (
                    rec.status in _TERMINAL_SHORT
                    and anchor < now - _SHORT_RETENTION
                ) or (
                    rec.status == JobStatus.DEAD
                    and anchor < now - _DEAD_RETENTION
                ):
                    del self._records[job_id]
                    removed += 1
        return removed

    async def active_chain_keys(self) -> set[tuple[str, str]]:
        async with self._mutex:
            return {
                (rec.kind, rec.character_id)
                for rec in self._records.values()
                if rec.status in ACTIVE_STATUSES and rec.character_id is not None
            }

    async def dead_chain_keys(self) -> set[tuple[str, str]]:
        async with self._mutex:
            return {
                (rec.kind, rec.character_id)
                for rec in self._records.values()
                if rec.status == JobStatus.DEAD and rec.character_id is not None
            }

    async def get(self, job_id: str) -> BackgroundJob | None:
        async with self._mutex:
            rec = self._records.get(job_id)
            return _to_job(rec) if rec is not None else None


@dataclass(slots=True)
class _LeaseRecord:
    owner_id: str
    epoch: int
    lease_until: datetime


class InMemoryBackgroundCoordinatorLease(BackgroundCoordinatorLeasePort):
    """In-process single-leader lease with a monotonic epoch."""

    def __init__(self) -> None:
        self._leases: dict[str, _LeaseRecord] = {}
        self._mutex = asyncio.Lock()

    async def acquire(
        self, name: str, owner_id: str, *, ttl_seconds: int, now: datetime,
    ) -> int | None:
        now = ensure_utc(now)
        until = now + timedelta(seconds=ttl_seconds)
        async with self._mutex:
            rec = self._leases.get(name)
            if rec is None:
                # First acquisition is itself an ownership change → epoch 1.
                self._leases[name] = _LeaseRecord(owner_id, 1, until)
                return 1
            if rec.owner_id == owner_id and rec.lease_until >= now:
                # Same owner over a still-LIVE lease → renew, epoch kept.
                rec.lease_until = until
                return rec.epoch
            if rec.lease_until > now:
                return None  # live lease held by another owner
            # Free / expired (INCLUDING same owner over an expired lease — a new
            # ownership incarnation) → ownership change, epoch bumps.
            rec.epoch += 1
            rec.owner_id = owner_id
            rec.lease_until = until
            return rec.epoch

    async def renew(
        self, name: str, owner_id: str, *, ttl_seconds: int, now: datetime,
    ) -> int | None:
        now = ensure_utc(now)
        async with self._mutex:
            rec = self._leases.get(name)
            if (
                rec is None
                or rec.owner_id != owner_id
                or rec.lease_until < now
            ):
                # Not the owner, or the lease already expired: an expired lease
                # cannot be renewed even by its original owner — re-acquire.
                return None
            rec.lease_until = now + timedelta(seconds=ttl_seconds)
            return rec.epoch

    async def release(self, name: str, owner_id: str) -> bool:
        async with self._mutex:
            rec = self._leases.get(name)
            if rec is None or rec.owner_id != owner_id:
                return False
            # Keep the epoch (monotonic); vacate ownership so the next
            # acquire is treated as a change and bumps it.
            rec.owner_id = ""
            rec.lease_until = _EPOCH_ZERO
            return True

    async def current(
        self, name: str,
    ) -> tuple[str, int, datetime] | None:
        async with self._mutex:
            rec = self._leases.get(name)
            if rec is None:
                return None
            return (rec.owner_id, rec.epoch, rec.lease_until)


class InMemoryTickJournal(TickJournalPort):
    """In-process scheduler-tick journal."""

    def __init__(self) -> None:
        self._entries: dict[tuple[int, str, str | None], datetime] = {}
        self._mutex = asyncio.Lock()

    async def record(
        self, entries: list[tuple[int, str, str | None]], *, now: datetime,
    ) -> None:
        now = ensure_utc(now)
        async with self._mutex:
            for bucket, kind, character_id in entries:
                key = (bucket, kind, character_id)
                self._entries.setdefault(key, now)

    async def list_range(
        self, *, from_bucket: int, to_bucket: int,
    ) -> list[tuple[int, str, str | None, datetime]]:
        async with self._mutex:
            rows = [
                (bucket, kind, character_id, recorded_at)
                for (bucket, kind, character_id), recorded_at in self._entries.items()
                if from_bucket <= bucket <= to_bucket
            ]
        rows.sort(key=lambda e: (e[0], e[1], e[2] is not None, e[2] or "", e[3]))
        return rows

    async def earliest_bucket_after(
        self, *, after_bucket: int, to_bucket: int,
    ) -> int | None:
        async with self._mutex:
            buckets = (
                bucket
                for bucket, _kind, _character_id in self._entries
                if after_bucket < bucket <= to_bucket
            )
            return min(buckets, default=None)

    async def list_bucket(
        self, bucket: int,
    ) -> list[tuple[int, str, str | None, datetime]]:
        return await self.list_range(from_bucket=bucket, to_bucket=bucket)

    async def prune(self, *, now: datetime) -> int:
        cutoff = ensure_utc(now) - _SHORT_RETENTION
        async with self._mutex:
            before = len(self._entries)
            self._entries = {
                key: recorded_at
                for key, recorded_at in self._entries.items()
                if recorded_at >= cutoff
            }
            return before - len(self._entries)


class InMemoryBackgroundCoordinatorCursor(BackgroundCoordinatorCursorPort):
    """In-process durable coordinator cursors.

    The cursor and lease stores share one lock so leadership validation and the
    monotonic cursor write are one atomic in-memory operation.
    """

    def __init__(
        self,
        lease: InMemoryBackgroundCoordinatorLease | None = None,
    ) -> None:
        self._cursors: dict[str, dict[str, Any]] = {}
        self._lease = lease
        self._mutex = lease._mutex if lease is not None else asyncio.Lock()

    async def get(self, name: str) -> Mapping[str, Any] | None:
        async with self._mutex:
            cursor = self._cursors.get(name)
            return dict(cursor) if cursor is not None else None

    async def set(self, name: str, cursor: Mapping[str, Any]) -> None:
        async with self._mutex:
            self._cursors[name] = dict(cursor)

    async def advance_if_leader(
        self,
        cursor_name: str,
        candidate_bucket: int,
        lease_name: str,
        owner_id: str,
        epoch: int,
        now: datetime,
    ) -> bool:
        now = ensure_utc(now)
        async with self._mutex:
            if self._lease is None:
                return False
            lease = self._lease._leases.get(lease_name)
            if (
                lease is None
                or lease.owner_id != owner_id
                or lease.epoch != epoch
                or lease.lease_until < now
            ):
                return False
            cursor = dict(self._cursors.get(cursor_name, {}))
            current = cursor.get("last_bucket")
            if not isinstance(current, int) or candidate_bucket > current:
                cursor["last_bucket"] = candidate_bucket
                self._cursors[cursor_name] = cursor
            elif cursor_name not in self._cursors:
                self._cursors[cursor_name] = cursor
            return True

    async def claim_due(
        self, name: str, owner_id: str = "", *, now: datetime,
        interval_seconds: float, reservation_seconds: float = 300.0,
    ) -> bool:
        async with self._mutex:
            cursor = self._cursors.get(name)
            cursor = dict(cursor) if cursor else {}
            last = cursor.get("last_success", cursor.get("last_run"))
            reserved_until = cursor.get("reserved_until")
            if (
                isinstance(reserved_until, (int, float))
                and float(reserved_until) > now.timestamp()
            ) or (
                isinstance(last, (int, float))
                and now.timestamp() - float(last) < interval_seconds
            ):
                return False
            cursor["reserved_by"] = owner_id
            cursor["reserved_until"] = (now + timedelta(seconds=reservation_seconds)).timestamp()
            self._cursors[name] = cursor
            return True

    async def complete_due(
        self, name: str, owner_id: str, *, completed_at: datetime,
    ) -> bool:
        async with self._mutex:
            cursor = self._cursors.get(name)
            if cursor is None or cursor.get("reserved_by") != owner_id:
                return False
            cursor = dict(cursor)
            cursor["last_success"] = ensure_utc(completed_at).timestamp()
            cursor.pop("last_run", None)
            cursor.pop("reserved_by", None)
            cursor.pop("reserved_until", None)
            self._cursors[name] = cursor
            return True

    async def release_due(self, name: str, owner_id: str) -> bool:
        async with self._mutex:
            cursor = self._cursors.get(name)
            if cursor is None or cursor.get("reserved_by") != owner_id:
                return False
            cursor = dict(cursor)
            cursor.pop("reserved_by", None)
            cursor.pop("reserved_until", None)
            self._cursors[name] = cursor
            return True



class InMemoryRuntimeOwnership(RuntimeOwnershipPort):
    """In-process execution-ownership row with an epoch CAS flip.

    Parity twin of ``SABackgroundExecutionMode``: an absent row reads
    ``('embedded', 0)``; ``flip`` succeeds only when the current epoch equals
    ``expected_epoch``. The mutex makes concurrent flips against the same epoch
    resolve to exactly one winner, mirroring the SA ``SELECT ... FOR UPDATE``.
    """

    def __init__(self) -> None:
        self._mode: str | None = None
        self._epoch: int = ABSENT_EPOCH
        self._mutex = asyncio.Lock()

    async def get(self) -> tuple[str, int]:
        async with self._mutex:
            if self._mode is None:
                return (ABSENT_MODE, ABSENT_EPOCH)
            return (self._mode, self._epoch)

    async def flip(
        self, target_mode: str, expected_epoch: int, *, now: datetime,
    ) -> int | None:
        if target_mode not in VALID_MODES:
            raise ValueError(f"invalid execution mode: {target_mode!r}")
        async with self._mutex:
            current_epoch = (
                ABSENT_EPOCH if self._mode is None else self._epoch
            )
            if current_epoch != expected_epoch:
                return None
            self._mode = target_mode
            self._epoch = expected_epoch + 1
            return self._epoch


def _to_claimed(rec: _JobRecord) -> ClaimedJob:
    assert rec.lease_owner is not None and rec.lease_until is not None
    return ClaimedJob(
        id=rec.id,
        kind=rec.kind,
        attempt_count=rec.attempt_count,
        max_attempts=rec.max_attempts,
        fencing_epoch=rec.fencing_epoch,
        idempotency_key=rec.idempotency_key,
        priority=rec.priority,
        due_at=rec.due_at,
        payload_version=rec.payload_version,
        payload=dict(rec.payload),
        lease_owner=rec.lease_owner,
        lease_until=rec.lease_until,
        tenant_id=rec.tenant_id,
        operator_id=rec.operator_id,
        character_id=rec.character_id,
    )


def _to_job(rec: _JobRecord) -> BackgroundJob:
    return BackgroundJob(
        id=rec.id,
        kind=rec.kind,
        status=rec.status,
        idempotency_key=rec.idempotency_key,
        due_at=rec.due_at,
        priority=rec.priority,
        fencing_epoch=rec.fencing_epoch,
        attempt_count=rec.attempt_count,
        max_attempts=rec.max_attempts,
        payload_version=rec.payload_version,
        payload=dict(rec.payload),
        created_at=rec.created_at,
        updated_at=rec.updated_at,
        tenant_id=rec.tenant_id,
        operator_id=rec.operator_id,
        character_id=rec.character_id,
        lease_owner=rec.lease_owner,
        lease_until=rec.lease_until,
        last_error=rec.last_error,
        outcome=dict(rec.outcome) if rec.outcome is not None else None,
        claimed_at=rec.claimed_at,
        finished_at=rec.finished_at,
    )
