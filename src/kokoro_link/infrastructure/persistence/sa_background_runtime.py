"""SQLAlchemy adapters for background runtime coordination (P2-A).

Groups the three small non-queue ports that back the distributed
coordinator: the single-leader lease (monotonic fencing epoch), the
due-discovery cursors, and the scheduler-tick parity journal. Kept in one
module because each is a thin table and they share the same lifecycle.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker

from kokoro_link.contracts.clock import ensure_utc
from kokoro_link.contracts.execution_mode import (
    ABSENT_EPOCH,
    ABSENT_MODE,
    EXECUTION_MODE_ROW_NAME,
    VALID_MODES,
)
from kokoro_link.infrastructure.persistence.models import (
    BackgroundCoordinatorCursorRow,
    BackgroundExecutionModeRow,
    BackgroundRuntimeLeaseRow,
    SchedulerTickJournalRow,
)


_LOGGER = logging.getLogger(__name__)

_JOURNAL_RETENTION = timedelta(days=7)
_EPOCH_ZERO = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SABackgroundCoordinatorLease:
    """TTL leader lease with a monotonic, never-reset fencing epoch.

    Ownership transitions are serialized with ``SELECT ... FOR UPDATE`` on
    the lease row so two would-be coordinators cannot both win a takeover.
    """

    def __init__(self, session_factory: sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def acquire(
        self, name: str, owner_id: str, *, ttl_seconds: int, now: datetime,
    ) -> int | None:
        now = ensure_utc(now)
        until = now + timedelta(seconds=ttl_seconds)
        async with self._session_factory() as session:
            row = await session.get(
                BackgroundRuntimeLeaseRow, name, with_for_update=True,
            )
            if row is None:
                session.add(BackgroundRuntimeLeaseRow(
                    name=name,
                    owner_id=owner_id,
                    epoch=1,
                    lease_until=until,
                    updated_at=now,
                ))
                try:
                    await session.commit()
                    return 1
                except IntegrityError:
                    # A concurrent cold-start inserted the lease row first
                    # (FOR UPDATE cannot gap-lock a not-yet-existent PK). Roll
                    # back and fall through to the normal takeover/hold path
                    # against the row the winner created, instead of leaking
                    # the PK violation to the caller.
                    await session.rollback()
                    row = await session.get(
                        BackgroundRuntimeLeaseRow, name, with_for_update=True,
                    )
                    if row is None:
                        return None
            if row.owner_id == owner_id and ensure_utc(row.lease_until) >= now:
                # Same owner over a still-LIVE lease → renew, epoch kept.
                row.lease_until = until
                row.updated_at = now
                await session.commit()
                return row.epoch
            if ensure_utc(row.lease_until) > now:
                # Live lease held by a DIFFERENT owner → denied.
                await session.rollback()
                return None
            # Free / expired (INCLUDING the same owner over an expired lease —
            # a new ownership incarnation whose stale predecessor must be fenced
            # out) → ownership change, epoch bumps.
            row.epoch += 1
            row.owner_id = owner_id
            row.lease_until = until
            row.updated_at = now
            await session.commit()
            return row.epoch

    async def renew(
        self, name: str, owner_id: str, *, ttl_seconds: int, now: datetime,
    ) -> int | None:
        now = ensure_utc(now)
        async with self._session_factory() as session:
            row = await session.get(
                BackgroundRuntimeLeaseRow, name, with_for_update=True,
            )
            if (
                row is None
                or row.owner_id != owner_id
                or ensure_utc(row.lease_until) < now
            ):
                # Not the owner, or the lease already expired: an expired lease
                # cannot be renewed even by its original owner — it must be
                # re-acquired (which bumps the epoch), so a slow/partitioned
                # owner cannot silently keep the old epoch alive.
                await session.rollback()
                return None
            row.lease_until = now + timedelta(seconds=ttl_seconds)
            row.updated_at = now
            await session.commit()
            return row.epoch

    async def release(self, name: str, owner_id: str) -> bool:
        async with self._session_factory() as session:
            row = await session.get(
                BackgroundRuntimeLeaseRow, name, with_for_update=True,
            )
            if row is None or row.owner_id != owner_id:
                await session.rollback()
                return False
            # Keep the epoch (monotonic); vacate ownership so the next
            # acquire counts as a change and bumps it.
            row.owner_id = ""
            row.lease_until = _EPOCH_ZERO
            row.updated_at = _utcnow()
            await session.commit()
            return True

    async def current(
        self, name: str,
    ) -> tuple[str, int, datetime] | None:
        async with self._session_factory() as session:
            row = await session.get(BackgroundRuntimeLeaseRow, name)
            if row is None:
                return None
            return (row.owner_id, row.epoch, ensure_utc(row.lease_until))


class SABackgroundCoordinatorCursor:
    """Durable key/value coordinator cursors (JSON of IDs / timestamps)."""

    def __init__(self, session_factory: sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get(self, name: str) -> Mapping[str, Any] | None:
        async with self._session_factory() as session:
            row = await session.get(BackgroundCoordinatorCursorRow, name)
            if row is None:
                return None
            return _load(row.cursor_json)

    async def set(self, name: str, cursor: Mapping[str, Any]) -> None:
        now = _utcnow()
        async with self._session_factory() as session:
            row = await session.get(BackgroundCoordinatorCursorRow, name)
            if row is None:
                session.add(BackgroundCoordinatorCursorRow(
                    name=name, cursor_json=_dump(cursor), updated_at=now,
                ))
            else:
                row.cursor_json = _dump(cursor)
                row.updated_at = now
            await session.commit()

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
        async with self._session_factory() as session:
            lease = await session.get(
                BackgroundRuntimeLeaseRow, lease_name, with_for_update=True,
            )
            if (
                lease is None
                or lease.owner_id != owner_id
                or lease.epoch != epoch
                or ensure_utc(lease.lease_until) < now
            ):
                await session.rollback()
                return False
            row = await session.get(
                BackgroundCoordinatorCursorRow, cursor_name, with_for_update=True,
            )
            if row is None:
                session.add(BackgroundCoordinatorCursorRow(
                    name=cursor_name,
                    cursor_json=_dump({"last_bucket": candidate_bucket}),
                    updated_at=now,
                ))
            else:
                cursor = _load(row.cursor_json)
                current = cursor.get("last_bucket")
                if not isinstance(current, int) or candidate_bucket > current:
                    cursor["last_bucket"] = candidate_bucket
                    row.cursor_json = _dump(cursor)
                    row.updated_at = now
            try:
                await session.commit()
            except IntegrityError:
                # A cold-start cursor insert can race despite the lease lock;
                # the losing transaction must not claim advancement.
                await session.rollback()
                return False
            return True

    async def claim_due(
        self, name: str, owner_id: str = "", *, now: datetime,
        interval_seconds: float, reservation_seconds: float = 300.0,
    ) -> bool:
        now = ensure_utc(now)
        until = now + timedelta(seconds=reservation_seconds)
        async with self._session_factory() as session:
            row = await session.get(
                BackgroundCoordinatorCursorRow, name, with_for_update=True,
            )
            cursor = _load(row.cursor_json) if row else {}
            last = cursor.get("last_success", cursor.get("last_run"))
            reserved_until = cursor.get("reserved_until")
            live = isinstance(reserved_until, (int, float)) and float(reserved_until) > now.timestamp()
            if live or (isinstance(last, (int, float)) and now.timestamp() - float(last) < interval_seconds):
                await session.rollback()
                return False
            cursor["reserved_by"] = owner_id
            cursor["reserved_until"] = until.timestamp()
            if row is None:
                session.add(BackgroundCoordinatorCursorRow(
                    name=name, cursor_json=_dump(cursor), updated_at=now,
                ))
            else:
                row.cursor_json = _dump(cursor)
                row.updated_at = now
            await session.commit()
            return True

    async def complete_due(
        self, name: str, owner_id: str, *, completed_at: datetime,
    ) -> bool:
        completed_at = ensure_utc(completed_at)
        async with self._session_factory() as session:
            row = await session.get(
                BackgroundCoordinatorCursorRow, name, with_for_update=True,
            )
            if row is None:
                await session.rollback()
                return False
            cursor = _load(row.cursor_json)
            if cursor.get("reserved_by") != owner_id:
                await session.rollback()
                return False
            cursor["last_success"] = completed_at.timestamp()
            cursor.pop("last_run", None)
            cursor.pop("reserved_by", None)
            cursor.pop("reserved_until", None)
            row.cursor_json = _dump(cursor)
            row.updated_at = completed_at
            await session.commit()
            return True

    async def release_due(self, name: str, owner_id: str) -> bool:
        async with self._session_factory() as session:
            row = await session.get(
                BackgroundCoordinatorCursorRow, name, with_for_update=True,
            )
            if row is None:
                await session.rollback()
                return False
            cursor = _load(row.cursor_json)
            if cursor.get("reserved_by") != owner_id:
                await session.rollback()
                return False
            cursor.pop("reserved_by", None)
            cursor.pop("reserved_until", None)
            row.cursor_json = _dump(cursor)
            row.updated_at = _utcnow()
            await session.commit()
            return True



class SATickJournal:
    """Append-only scheduler-tick journal (embedded/distributed parity)."""

    def __init__(self, session_factory: sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def record(
        self, entries: list[tuple[int, str, str | None]], *, now: datetime,
    ) -> None:
        if not entries:
            return
        now = ensure_utc(now)
        async with self._session_factory() as session:
            values = [
                {
                    "tick_bucket": bucket,
                    "kind": kind,
                    "character_id": character_id,
                    "recorded_at": now,
                }
                for bucket, kind, character_id in entries
            ]
            bind = session.bind
            if bind is not None and bind.dialect.name == "postgresql":
                await session.execute(
                    pg_insert(SchedulerTickJournalRow)
                    .values(values)
                    .on_conflict_do_nothing()
                )
                await session.commit()
            else:
                # H2/SQLite-compatible fallback: commit each candidate so a
                # duplicate rollback cannot erase earlier successful rows.
                for value in values:
                    try:
                        session.add(SchedulerTickJournalRow(**value))
                        await session.commit()
                    except IntegrityError:
                        await session.rollback()

    async def list_range(
        self, *, from_bucket: int, to_bucket: int,
    ) -> list[tuple[int, str, str | None, datetime]]:
        stmt = (
            select(SchedulerTickJournalRow)
            .where(
                SchedulerTickJournalRow.tick_bucket >= from_bucket,
                SchedulerTickJournalRow.tick_bucket <= to_bucket,
            )
            .order_by(
                SchedulerTickJournalRow.tick_bucket.asc(),
                SchedulerTickJournalRow.kind.asc(),
                SchedulerTickJournalRow.character_id.asc().nulls_last(),
                SchedulerTickJournalRow.recorded_at.asc(),
                SchedulerTickJournalRow.id.asc(),
            )
        )
        async with self._session_factory() as session:
            rows = (await session.execute(stmt)).scalars().all()
            return [
                (
                    row.tick_bucket,
                    row.kind,
                    row.character_id,
                    ensure_utc(row.recorded_at),
                )
                for row in rows
            ]

    async def earliest_bucket_after(
        self, *, after_bucket: int, to_bucket: int,
    ) -> int | None:
        stmt = select(func.min(SchedulerTickJournalRow.tick_bucket)).where(
            SchedulerTickJournalRow.tick_bucket > after_bucket,
            SchedulerTickJournalRow.tick_bucket <= to_bucket,
        )
        async with self._session_factory() as session:
            return (await session.execute(stmt)).scalar_one_or_none()

    async def list_bucket(
        self, bucket: int,
    ) -> list[tuple[int, str, str | None, datetime]]:
        stmt = (
            select(SchedulerTickJournalRow)
            .where(SchedulerTickJournalRow.tick_bucket == bucket)
            .order_by(
                SchedulerTickJournalRow.kind.asc(),
                SchedulerTickJournalRow.character_id.asc().nulls_last(),
                SchedulerTickJournalRow.recorded_at.asc(),
                SchedulerTickJournalRow.id.asc(),
            )
        )
        async with self._session_factory() as session:
            rows = (await session.execute(stmt)).scalars().all()
            return [
                (
                    row.tick_bucket,
                    row.kind,
                    row.character_id,
                    ensure_utc(row.recorded_at),
                )
                for row in rows
            ]

    async def prune(self, *, now: datetime) -> int:
        cutoff = ensure_utc(now) - _JOURNAL_RETENTION
        async with self._session_factory() as session:
            result = await session.execute(
                delete(SchedulerTickJournalRow).where(
                    SchedulerTickJournalRow.recorded_at < cutoff,
                ),
            )
            await session.commit()
            return int(result.rowcount or 0)


class SABackgroundExecutionMode:
    """Runtime execution-ownership row with a monotonic-epoch CAS flip.

    Reads resolve an absent row to ``('embedded', 0)`` (the self-host red
    line). ``flip`` is a compare-and-swap serialized with ``SELECT ... FOR
    UPDATE`` on the single row so two concurrent flips against the same epoch
    cannot both win — exactly one bumps the epoch, the loser gets ``None``.
    """

    def __init__(self, session_factory: sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get(self) -> tuple[str, int]:
        async with self._session_factory() as session:
            row = await session.get(
                BackgroundExecutionModeRow, EXECUTION_MODE_ROW_NAME,
            )
            if row is None:
                return (ABSENT_MODE, ABSENT_EPOCH)
            return (row.mode, row.epoch)

    async def flip(
        self, target_mode: str, expected_epoch: int, *, now: datetime,
    ) -> int | None:
        now = ensure_utc(now)
        if target_mode not in VALID_MODES:
            raise ValueError(f"invalid execution mode: {target_mode!r}")
        new_epoch = expected_epoch + 1
        async with self._session_factory() as session:
            row = await session.get(
                BackgroundExecutionModeRow,
                EXECUTION_MODE_ROW_NAME,
                with_for_update=True,
            )
            if row is None:
                # Absent row == epoch 0. Only a flip that expects epoch 0 may
                # INSERT it; any other expectation is a stale CAS.
                if expected_epoch != ABSENT_EPOCH:
                    await session.rollback()
                    return None
                session.add(BackgroundExecutionModeRow(
                    name=EXECUTION_MODE_ROW_NAME,
                    mode=target_mode,
                    epoch=new_epoch,
                    updated_at=now,
                ))
                try:
                    await session.commit()
                    return new_epoch
                except IntegrityError:
                    # A concurrent flip inserted the row first (FOR UPDATE
                    # cannot gap-lock a not-yet-existent PK). This flip lost the
                    # race — the winner already bumped the epoch, so our
                    # expected_epoch=0 is now stale.
                    await session.rollback()
                    return None
            if row.epoch != expected_epoch:
                await session.rollback()
                return None
            row.mode = target_mode
            row.epoch = new_epoch
            row.updated_at = now
            await session.commit()
            return new_epoch


def _dump(payload: Mapping[str, Any]) -> str:
    try:
        return json.dumps(dict(payload), ensure_ascii=False)
    except (TypeError, ValueError):
        _LOGGER.exception("coordinator cursor payload not serializable")
        return "{}"


def _load(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}
