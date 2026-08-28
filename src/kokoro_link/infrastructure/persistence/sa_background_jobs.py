"""SQLAlchemy adapter for the durable background-job queue (P2-A).

PostgreSQL-first. ``claim`` uses ``SELECT ... FOR UPDATE SKIP LOCKED`` so
concurrent workers pull disjoint job sets without ever blocking on each
other, and never hold a DB transaction across the (long) handler run.
Idempotency is enforced by the ``uq_background_jobs_active_idem`` partial
unique index — a duplicate active enqueue surfaces as ``IntegrityError``
and is reported as the idempotent ``None`` result, not an error.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping
from uuid import uuid4

from sqlalchemy import and_, delete, func, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased, sessionmaker

from kokoro_link.contracts.background_jobs import (
    COORDINATOR_LEASE_NAME,
    BackgroundJob,
    BackgroundJobSpec,
    ClaimedJob,
    JobOutcome,
    JobStatus,
    QueueStats,
    summarize_error,
)
from kokoro_link.contracts.clock import ensure_utc
from kokoro_link.contracts.due_jobs import capability_kind_sets
from kokoro_link.infrastructure.persistence.models import (
    BackgroundJobRow,
    BackgroundRuntimeLeaseRow,
)


_LOGGER = logging.getLogger(__name__)

# The partial unique index that enforces at-most-one active row per
# ``idempotency_key``. Only a violation of THIS constraint is the benign
# idempotent/dedup path; any other IntegrityError is a real defect and must
# surface, never be masked as a successful dedupe.
_ACTIVE_IDEM_CONSTRAINT = "uq_background_jobs_active_idem"

_TERMINAL_SHORT = (
    JobStatus.DONE.value,
    JobStatus.FAILED.value,
    JobStatus.SUPERSEDED.value,
)
_SHORT_RETENTION = timedelta(days=7)
_DEAD_RETENTION = timedelta(days=30)

# §5/§7 claim-admission serialisation. ``claim`` decides capability caps AND
# tenant-busy admission from the *committed* live-claimed counts; two workers
# whose claim transactions overlap each read the same pre-commit snapshot
# (each sees ``inflight=0``), lock DISJOINT candidate rows via ``FOR UPDATE SKIP
# LOCKED`` and both commit — so a cap of 1 becomes 2 (and a tenant runs two
# jobs at once). ``SKIP LOCKED`` locks only the candidate job row, never the
# admission-count rows, so it cannot serialise this. A single transaction-scoped
# advisory lock, taken at the top of every claim transaction on PostgreSQL,
# serialises admission end-to-end: the second claimer blocks until the first
# COMMITs and then reads its committed claims. Released automatically at
# commit/rollback (``pg_advisory_xact_lock``). Distributed-only: the embedded
# self-host never runs the SA queue, and non-PostgreSQL binds (unit fakes) skip
# the lock entirely, so behaviour there is unchanged.
_CLAIM_ADMISSION_LOCK_KEY = 6_845_121_207_003_311_001


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _is_active_idem_violation(error: IntegrityError) -> bool:
    """True only when ``error`` is a violation of the active-idempotency
    index (the benign dedup/fenced path). Any other integrity failure is a
    real defect and must not be swallowed as a no-op."""
    return _ACTIVE_IDEM_CONSTRAINT in str(getattr(error, "orig", error))


class SABackgroundJobQueue:
    def __init__(self, session_factory: sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def enqueue(
        self, spec: BackgroundJobSpec, *, now: datetime,
    ) -> str | None:
        now = ensure_utc(now)
        async with self._session_factory() as session:
            # Re-read the coordinator lease in THIS transaction UNDER A ROW
            # LOCK (``FOR UPDATE``) so the fencing decision is atomic against
            # a concurrent takeover (§3.2): the takeover's acquire() also
            # takes ``FOR UPDATE`` on this row, so the two serialise. A plain
            # ``get`` would let a stale ex-coordinator read the pre-takeover
            # epoch and then insert AFTER the takeover committed. The lease
            # must be (a) present, (b) still live at ``now`` (an owner whose
            # own lease expired is no longer the leader), and (c) carry
            # exactly ``spec.fencing_epoch``.
            lease = await session.get(
                BackgroundRuntimeLeaseRow, COORDINATOR_LEASE_NAME,
                with_for_update=True,
            )
            # Snapshot the fencing inputs BEFORE any rollback: after a rollback
            # the ORM row is expired and touching an attribute would trigger a
            # lazy DB load outside the async greenlet (MissingGreenlet).
            current_epoch = lease.epoch if lease is not None else None
            lease_live = (
                lease is not None and ensure_utc(lease.lease_until) >= now
            )
            if lease is None or not lease_live or current_epoch != spec.fencing_epoch:
                await session.rollback()
                _LOGGER.warning(
                    "background enqueue rejected: stale/expired fencing epoch "
                    "kind=%s spec_epoch=%s current_epoch=%s live=%s",
                    spec.kind, spec.fencing_epoch, current_epoch, lease_live,
                )
                return None
            job_id = uuid4().hex
            session.add(BackgroundJobRow(
                id=job_id,
                kind=spec.kind,
                tenant_id=spec.tenant_id,
                operator_id=spec.operator_id,
                character_id=spec.character_id,
                due_at=ensure_utc(spec.due_at),
                priority=spec.priority,
                status=JobStatus.QUEUED.value,
                attempt_count=0,
                max_attempts=spec.max_attempts,
                idempotency_key=spec.idempotency_key,
                fencing_epoch=spec.fencing_epoch,
                payload_version=spec.payload_version,
                payload_json=_dump(spec.payload),
                created_at=now,
                updated_at=now,
            ))
            try:
                await session.commit()
            except IntegrityError as exc:
                # Active idempotency_key already present — idempotent, not
                # an error. Covers the concurrent double-enqueue race too.
                # Any OTHER integrity failure is a real defect: re-raise it
                # rather than silently masking a lost job as a dedupe.
                await session.rollback()
                if not _is_active_idem_violation(exc):
                    raise
                return None
            return job_id

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
        # §5 capability caps: (capability, cap, kinds) triples for the active
        # ceilings. The per-iteration WHERE excludes any candidate whose kind
        # maps to a capability already at its cap. The live-claimed count
        # subquery re-runs each iteration and — via autoflush — already includes
        # the rows claimed earlier in THIS batch (their status is set to CLAIMED
        # in-session), so no separate in-batch counter is needed; across workers
        # it counts committed rows (same posture as tenant_busy).
        capped = capability_kind_sets(dict(capability_caps)) if capability_caps else ()
        async with self._session_factory() as session:
            # Serialise admission across concurrently-claiming workers (see
            # ``_CLAIM_ADMISSION_LOCK_KEY``): without this, two overlapping claim
            # transactions each read ``inflight=0`` and both admit, breaking the
            # capability cap and the tenant-busy ceiling. No-op off PostgreSQL.
            await self._lock_claim_admission(session)
            claimed: list[ClaimedJob] = []
            selected_tenants: set[str] = set()
            while len(claimed) < limit:
                other = aliased(BackgroundJobRow)
                tenant_busy = select(other.id).where(
                    other.tenant_id == BackgroundJobRow.tenant_id,
                    other.id != BackgroundJobRow.id,
                    other.status == JobStatus.CLAIMED.value,
                    other.lease_until.is_not(None),
                    other.lease_until >= now,
                ).exists()
                conditions = [
                    or_(
                        and_(
                            BackgroundJobRow.status == JobStatus.QUEUED.value,
                            BackgroundJobRow.due_at <= now,
                        ),
                        and_(
                            BackgroundJobRow.status == JobStatus.CLAIMED.value,
                            BackgroundJobRow.lease_until.is_not(None),
                            BackgroundJobRow.lease_until <= now,
                        ),
                    ),
                    or_(BackgroundJobRow.tenant_id.is_(None), ~tenant_busy),
                ]
                if selected_tenants:
                    conditions.append(
                        or_(
                            BackgroundJobRow.tenant_id.is_(None),
                            ~BackgroundJobRow.tenant_id.in_(selected_tenants),
                        ),
                    )
                for _capability, cap, kinds in capped:
                    kind_list = list(kinds)
                    if cap <= 0:
                        conditions.append(~BackgroundJobRow.kind.in_(kind_list))
                        continue
                    # Live-claimed count for this capability's kinds (includes the
                    # rows claimed earlier in this batch via autoflush).
                    cap_row = aliased(BackgroundJobRow)
                    inflight = (
                        select(func.count())
                        .where(
                            cap_row.kind.in_(kind_list),
                            cap_row.status == JobStatus.CLAIMED.value,
                            cap_row.lease_until.is_not(None),
                            cap_row.lease_until >= now,
                        )
                        .scalar_subquery()
                    )
                    conditions.append(
                        or_(
                            ~BackgroundJobRow.kind.in_(kind_list),
                            inflight < cap,
                        ),
                    )
                stmt = (
                    select(BackgroundJobRow)
                    .where(*conditions)
                    .order_by(
                        BackgroundJobRow.priority.asc(),
                        BackgroundJobRow.due_at.asc(),
                        BackgroundJobRow.id.asc(),
                    )
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
                if capped:
                    # Make the rows claimed earlier in this batch visible to the
                    # capability count subquery independently of session autoflush.
                    await session.flush()
                row = (await session.execute(stmt)).scalar_one_or_none()
                if row is None:
                    break
                reclaim = row.status == JobStatus.CLAIMED.value
                if reclaim and row.attempt_count >= row.max_attempts:
                    row.status = JobStatus.DEAD.value
                    row.last_error = row.last_error or summarize_error(
                        "lease expired: max attempts exhausted",
                    )
                    row.lease_owner = None
                    row.lease_until = None
                    row.finished_at = now
                    row.updated_at = now
                    continue
                row.status = JobStatus.CLAIMED.value
                row.attempt_count += 1
                row.lease_owner = worker_id
                row.lease_until = lease_until
                row.claimed_at = now
                row.updated_at = now
                if row.tenant_id is not None:
                    selected_tenants.add(row.tenant_id)
                claimed.append(_row_to_claimed(row))
            await session.commit()
            return claimed

    @staticmethod
    async def _lock_claim_admission(session: AsyncSession) -> None:
        """Take the transaction-scoped claim-admission advisory lock (PG only).

        ``pg_advisory_xact_lock`` blocks until the lock is free and releases it
        automatically when the surrounding transaction commits/rolls back, so the
        whole ``claim`` runs single-file and every worker reads a committed
        admission snapshot. On SQLite / other binds (unit fakes never exercise the
        cap race) the function is a no-op — the lock does not exist there and the
        distributed queue only ever runs on PostgreSQL."""
        bind = session.bind
        if bind is None or bind.dialect.name != "postgresql":
            return
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": _CLAIM_ADMISSION_LOCK_KEY},
        )

    async def extend_lease(
        self, job_id: str, worker_id: str, *, until: datetime, now: datetime,
    ) -> bool:
        now = ensure_utc(now)
        async with self._session_factory() as session:
            result = await session.execute(
                update(BackgroundJobRow)
                .where(
                    BackgroundJobRow.id == job_id,
                    BackgroundJobRow.status == JobStatus.CLAIMED.value,
                    BackgroundJobRow.lease_owner == worker_id,
                    # An already-expired lease is not extendable by its former
                    # holder (parity with complete/fail): it must be re-claimed.
                    BackgroundJobRow.lease_until >= now,
                )
                .values(lease_until=ensure_utc(until), updated_at=now),
            )
            await session.commit()
            return result.rowcount == 1

    async def complete(
        self,
        job_id: str,
        worker_id: str,
        *,
        outcome: JobOutcome,
        now: datetime,
    ) -> bool:
        now = ensure_utc(now)
        async with self._session_factory() as session:
            # Deliberately NO coordinator-epoch re-check here. With the atomic
            # ``enqueue`` fencing (FOR UPDATE lease read), a job only exists if
            # it was inserted while its coordinator verifiably held the current
            # epoch — it was legitimate at insert time. A later takeover bumps
            # the epoch for FUTURE enqueues, but must NOT strand already-queued
            # jobs as un-completable; the lease-ownership guard below is what
            # protects completion, not the coordinator epoch.
            result = await session.execute(
                update(BackgroundJobRow)
                .where(
                    BackgroundJobRow.id == job_id,
                    BackgroundJobRow.status == JobStatus.CLAIMED.value,
                    BackgroundJobRow.lease_owner == worker_id,
                    BackgroundJobRow.lease_until >= now,
                )
                .values(
                    status=JobStatus.DONE.value,
                    outcome_json=_dump(outcome.detail),
                    finished_at=now,
                    updated_at=now,
                    lease_until=None,
                ),
            )
            await session.commit()
            return result.rowcount == 1

    async def release_claim(
        self, job_id: str, worker_id: str, *, now: datetime,
    ) -> bool:
        now = ensure_utc(now)
        async with self._session_factory() as session:
            # Same conditional-UPDATE shape as ``complete``: the WHERE clause
            # re-validates lease ownership atomically, so a concurrent reclaim
            # (whose UPDATE moved lease_owner) simply makes this a no-op rather
            # than requeueing a job another worker is already running. No row
            # lock needed — there is no read-modify-write branch here, and the
            # attempt refund is expressed as a SQL decrement.
            result = await session.execute(
                update(BackgroundJobRow)
                .where(
                    BackgroundJobRow.id == job_id,
                    BackgroundJobRow.status == JobStatus.CLAIMED.value,
                    BackgroundJobRow.lease_owner == worker_id,
                    BackgroundJobRow.lease_until >= now,
                )
                .values(
                    status=JobStatus.QUEUED.value,
                    attempt_count=BackgroundJobRow.attempt_count - 1,
                    lease_owner=None,
                    lease_until=None,
                    claimed_at=None,
                    updated_at=now,
                ),
            )
            await session.commit()
            return result.rowcount == 1

    async def withdraw_queued(
        self, idempotency_key: str, *, now: datetime,
    ) -> int:
        now = ensure_utc(now)
        async with self._session_factory() as session:
            # Conditional UPDATE, no row lock: ``status == queued`` in the
            # WHERE clause is what makes this lose the race against a
            # concurrent ``claim`` instead of stealing a job out of a
            # worker's hands. A row that got claimed first stays claimed and
            # the handler's own subject re-verification skips it.
            result = await session.execute(
                update(BackgroundJobRow)
                .where(
                    BackgroundJobRow.idempotency_key == idempotency_key,
                    BackgroundJobRow.status == JobStatus.QUEUED.value,
                )
                .values(
                    status=JobStatus.SUPERSEDED.value,
                    lease_owner=None,
                    lease_until=None,
                    finished_at=now,
                    updated_at=now,
                ),
            )
            await session.commit()
            return result.rowcount or 0

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
        async with self._session_factory() as session:
            # Lock the row for the read-modify-write. ``complete`` re-validates
            # ownership inside its conditional UPDATE; ``fail`` must branch on
            # attempt_count, so it takes the row lock instead — otherwise a
            # concurrent ``claim`` reclaim (SELECT FOR UPDATE SKIP LOCKED) could
            # slip between an unlocked read and this write, and the PK-only
            # UPDATE would clobber the reclaim, re-queueing a job another worker
            # is actively running.
            row = await session.get(
                BackgroundJobRow, job_id, with_for_update=True,
            )
            if (
                row is None
                or row.status != JobStatus.CLAIMED.value
                or row.lease_owner != worker_id
                or row.lease_until is None
                or ensure_utc(row.lease_until) < now
            ):
                await session.rollback()
                return False
            row.last_error = summarize_error(error)
            row.updated_at = now
            row.lease_owner = None
            row.lease_until = None
            if row.attempt_count >= row.max_attempts:
                row.status = JobStatus.DEAD.value
                row.finished_at = now
            elif retry_in_seconds is not None:
                row.status = JobStatus.QUEUED.value
                row.due_at = now + timedelta(seconds=retry_in_seconds)
                row.claimed_at = None
            else:
                row.status = JobStatus.FAILED.value
                row.finished_at = now
            await session.commit()
            return True

    async def stats(self, *, now: datetime) -> QueueStats:
        now = ensure_utc(now)
        async with self._session_factory() as session:
            status_rows = (await session.execute(
                select(BackgroundJobRow.status, func.count())
                .group_by(BackgroundJobRow.status),
            )).all()
            kind_rows = (await session.execute(
                select(BackgroundJobRow.kind, func.count())
                .group_by(BackgroundJobRow.kind),
            )).all()
            oldest_due = (await session.execute(
                select(func.min(BackgroundJobRow.due_at))
                .where(
                    BackgroundJobRow.status == JobStatus.QUEUED.value,
                    BackgroundJobRow.due_at <= now,
                ),
            )).scalar_one_or_none()
        status_counts = {status: int(count) for status, count in status_rows}
        kind_counts = {kind: int(count) for kind, count in kind_rows}
        oldest_age = (
            (now - ensure_utc(oldest_due)).total_seconds()
            if oldest_due is not None else 0.0
        )
        return QueueStats(
            status_counts=status_counts,
            kind_counts=kind_counts,
            oldest_queued_age_seconds=oldest_age,
            total=sum(status_counts.values()),
        )

    async def active_chain_keys(self) -> set[tuple[str, str]]:
        stmt = (
            select(BackgroundJobRow.kind, BackgroundJobRow.character_id)
            .where(
                BackgroundJobRow.status.in_(
                    (JobStatus.QUEUED.value, JobStatus.CLAIMED.value),
                ),
                BackgroundJobRow.character_id.is_not(None),
            )
            .distinct()
        )
        async with self._session_factory() as session:
            rows = (await session.execute(stmt)).all()
        return {(kind, character_id) for kind, character_id in rows}

    async def dead_chain_keys(self) -> set[tuple[str, str]]:
        stmt = (
            select(BackgroundJobRow.kind, BackgroundJobRow.character_id)
            .where(
                BackgroundJobRow.status == JobStatus.DEAD.value,
                BackgroundJobRow.character_id.is_not(None),
            )
            .distinct()
        )
        async with self._session_factory() as session:
            rows = (await session.execute(stmt)).all()
        return {(kind, character_id) for kind, character_id in rows}

    async def list_dead(self, *, limit: int) -> list[BackgroundJob]:
        stmt = (
            select(BackgroundJobRow)
            .where(BackgroundJobRow.status == JobStatus.DEAD.value)
            .order_by(BackgroundJobRow.finished_at.desc().nullslast())
            .limit(limit)
        )
        async with self._session_factory() as session:
            rows = (await session.execute(stmt)).scalars().all()
            return [_row_to_job(row) for row in rows]

    async def redrive(self, job_id: str) -> bool:
        async with self._session_factory() as session:
            try:
                result = await session.execute(
                    update(BackgroundJobRow)
                    .where(
                        BackgroundJobRow.id == job_id,
                        BackgroundJobRow.status == JobStatus.DEAD.value,
                    )
                    .values(
                        status=JobStatus.QUEUED.value,
                        attempt_count=0,
                        lease_owner=None,
                        lease_until=None,
                        last_error=None,
                        finished_at=None,
                        updated_at=_utcnow(),
                    ),
                )
                await session.commit()
            except IntegrityError as exc:
                # A fresh active job already carries this idempotency_key (the
                # logical key recurred while the row sat dead). Reviving would
                # break the active-idem invariant — the violation surfaces at
                # the UPDATE flush, not only at commit, so both are guarded.
                # Treat as a no-op redrive rather than crash the admin endpoint
                # with a 500.
                await session.rollback()
                if not _is_active_idem_violation(exc):
                    raise
                return False
            return result.rowcount == 1

    async def prune(self, *, now: datetime) -> int:
        now = ensure_utc(now)
        anchor = func.coalesce(
            BackgroundJobRow.finished_at, BackgroundJobRow.updated_at,
        )
        async with self._session_factory() as session:
            result = await session.execute(
                delete(BackgroundJobRow).where(
                    or_(
                        and_(
                            BackgroundJobRow.status.in_(_TERMINAL_SHORT),
                            anchor < now - _SHORT_RETENTION,
                        ),
                        and_(
                            BackgroundJobRow.status == JobStatus.DEAD.value,
                            anchor < now - _DEAD_RETENTION,
                        ),
                    ),
                ),
            )
            await session.commit()
            return int(result.rowcount or 0)

    async def get(self, job_id: str) -> BackgroundJob | None:
        async with self._session_factory() as session:
            row = await session.get(BackgroundJobRow, job_id)
            return _row_to_job(row) if row is not None else None


def _dump(payload: Any) -> str:
    try:
        return json.dumps(dict(payload), ensure_ascii=False)
    except (TypeError, ValueError):
        _LOGGER.exception("background job payload not serializable")
        return "{}"


def _load(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _row_to_claimed(row: BackgroundJobRow) -> ClaimedJob:
    return ClaimedJob(
        id=row.id,
        kind=row.kind,
        attempt_count=row.attempt_count,
        max_attempts=row.max_attempts,
        fencing_epoch=row.fencing_epoch,
        idempotency_key=row.idempotency_key,
        priority=row.priority,
        due_at=ensure_utc(row.due_at),
        payload_version=row.payload_version,
        payload=_load(row.payload_json),
        lease_owner=row.lease_owner or "",
        lease_until=ensure_utc(row.lease_until) if row.lease_until else _utcnow(),
        tenant_id=row.tenant_id,
        operator_id=row.operator_id,
        character_id=row.character_id,
    )


def _row_to_job(row: BackgroundJobRow) -> BackgroundJob:
    return BackgroundJob(
        id=row.id,
        kind=row.kind,
        status=JobStatus(row.status),
        idempotency_key=row.idempotency_key,
        due_at=ensure_utc(row.due_at),
        priority=row.priority,
        fencing_epoch=row.fencing_epoch,
        attempt_count=row.attempt_count,
        max_attempts=row.max_attempts,
        payload_version=row.payload_version,
        payload=_load(row.payload_json),
        created_at=ensure_utc(row.created_at),
        updated_at=ensure_utc(row.updated_at),
        tenant_id=row.tenant_id,
        operator_id=row.operator_id,
        character_id=row.character_id,
        lease_owner=row.lease_owner,
        lease_until=ensure_utc(row.lease_until) if row.lease_until else None,
        last_error=row.last_error,
        outcome=_load(row.outcome_json) if row.outcome_json else None,
        claimed_at=ensure_utc(row.claimed_at) if row.claimed_at else None,
        finished_at=ensure_utc(row.finished_at) if row.finished_at else None,
    )
