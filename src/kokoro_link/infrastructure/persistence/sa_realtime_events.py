"""SQLAlchemy adapter for the realtime transactional outbox (Phase 4 §7.1).

Implements :class:`RealtimeOutboxPort` against PostgreSQL. ``append`` writes on
the *caller's* session so the event row shares the domain write's transaction;
every other method opens its own short session (the ``sa_background_runtime``
convention). ``read_after`` issues the frontier read and the bounded overlap-tail
read as two separate index-served queries (never one ``OR ... LIMIT`` that would
let a saturated overlap window starve the forward frontier), leaning on the
``(created_at, id)`` composite index for the overlap-window range scan and the
primary key for the frontier keyset scan.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any, Mapping

from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker

from kokoro_link.contracts.clock import ensure_utc
from kokoro_link.contracts.realtime_events import (
    DEFAULT_OVERLAP,
    REALTIME_EVENT_CHANNEL,
    RealtimeEventSpec,
    StoredRealtimeEvent,
)
from kokoro_link.infrastructure.persistence.models import RealtimeEventRow


_LOGGER = logging.getLogger(__name__)


class SARealtimeOutbox:
    """PostgreSQL realtime-event outbox with out-of-order-safe replay reads."""

    def __init__(self, session_factory: sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def append(
        self, session: AsyncSession, spec: RealtimeEventSpec,
    ) -> StoredRealtimeEvent:
        created_at = ensure_utc(spec.created_at)
        row = RealtimeEventRow(
            tenant_id=spec.tenant_id,
            operator_id=spec.operator_id,
            event_kind=spec.event_kind,
            payload_json=_dump(spec.payload),
            created_at=created_at,
        )
        session.add(row)
        # Flush (not commit) to assign the autoincrement id under the caller's
        # transaction — a domain rollback discards this row with the rest.
        await session.flush()
        return StoredRealtimeEvent(
            id=row.id,
            event_kind=spec.event_kind,
            payload=dict(spec.payload),
            created_at=created_at,
            tenant_id=spec.tenant_id,
            operator_id=spec.operator_id,
        )

    async def read_after(
        self,
        cursor_id: int,
        *,
        overlap: timedelta = DEFAULT_OVERLAP,
        limit: int,
    ) -> list[StoredRealtimeEvent]:
        async with self._session_factory() as session:
            if cursor_id <= 0:
                stmt = (
                    select(RealtimeEventRow)
                    .order_by(RealtimeEventRow.id.asc())
                    .limit(limit)
                )
                rows = (await session.execute(stmt)).scalars().all()
                return [_to_event(row) for row in rows]

            # Anchor the overlap window to the cursor row's own created_at so
            # the read stays deterministic (no server-side now()) and re-reads
            # exactly the commit-lag tail around the frontier the caller reached.
            anchor = await session.get(RealtimeEventRow, cursor_id)
            if anchor is None:
                # Cursor row pruned: the time anchor is gone. Fall back to the
                # forward frontier only — the pruned tail is too old to matter.
                stmt = (
                    select(RealtimeEventRow)
                    .where(RealtimeEventRow.id > cursor_id)
                    .order_by(RealtimeEventRow.id.asc())
                    .limit(limit)
                )
                rows = (await session.execute(stmt)).scalars().all()
                return [_to_event(row) for row in rows]

            threshold = ensure_utc(anchor.created_at) - overlap
            # Two disjoint reads, NOT one ``OR ... LIMIT`` (which sorts the
            # overlap tail ahead of the frontier by id, so a full overlap window
            # of ``>= limit`` rows starves the frontier and the cursor wedges).
            # The frontier gets its own ``limit`` and always makes progress; the
            # overlap tail is bounded to the ``limit`` ids nearest the cursor.
            frontier_stmt = (
                select(RealtimeEventRow)
                .where(RealtimeEventRow.id > cursor_id)
                .order_by(RealtimeEventRow.id.asc())
                .limit(limit)
            )
            frontier = (await session.execute(frontier_stmt)).scalars().all()
            # Overlap tail: late-committing low ids inside the window. Ordered by
            # id DESC so the bound keeps the rows *closest* to the cursor (the
            # most-recently-assigned ids, i.e. those most likely still committing)
            # rather than the oldest of a burst; re-sorted ascending on return.
            overlap_stmt = (
                select(RealtimeEventRow)
                .where(
                    RealtimeEventRow.id <= cursor_id,
                    RealtimeEventRow.created_at >= threshold,
                )
                .order_by(RealtimeEventRow.id.desc())
                .limit(limit)
            )
            overlap_rows = (
                await session.execute(overlap_stmt)
            ).scalars().all()
            # Ids are disjoint across the two sets (<= cursor vs > cursor), so the
            # concatenation is already globally id-ascending with no cross de-dup.
            merged = list(reversed(overlap_rows)) + list(frontier)
            return [_to_event(row) for row in merged]

    async def latest_id(self) -> int:
        async with self._session_factory() as session:
            result = await session.execute(
                select(func.max(RealtimeEventRow.id)),
            )
            return int(result.scalar_one() or 0)

    async def notify_hint(self, *, event_id: int | None = None) -> None:
        payload = "" if event_id is None else str(event_id)
        try:
            async with self._session_factory() as session:
                await session.execute(
                    text("SELECT pg_notify(:channel, :payload)"),
                    {"channel": REALTIME_EVENT_CHANNEL, "payload": payload},
                )
                await session.commit()
        except Exception:  # noqa: BLE001 — a dropped wake hint is non-fatal
            # Best-effort only: a lost NOTIFY just makes a replica wait for its
            # next poll. Never let a hint failure escape into the domain path.
            _LOGGER.warning(
                "realtime outbox notify_hint failed (event_id=%s)",
                event_id,
                exc_info=True,
            )

    async def prune(self, older_than: datetime) -> int:
        cutoff = ensure_utc(older_than)
        async with self._session_factory() as session:
            result = await session.execute(
                delete(RealtimeEventRow).where(
                    RealtimeEventRow.created_at < cutoff,
                ),
            )
            await session.commit()
            return int(result.rowcount or 0)


def _to_event(row: RealtimeEventRow) -> StoredRealtimeEvent:
    return StoredRealtimeEvent(
        id=row.id,
        event_kind=row.event_kind,
        payload=_load(row.payload_json),
        created_at=ensure_utc(row.created_at),
        tenant_id=row.tenant_id,
        operator_id=row.operator_id,
    )


def _dump(payload: Mapping[str, Any]) -> str:
    try:
        return json.dumps(dict(payload), ensure_ascii=False)
    except (TypeError, ValueError):
        _LOGGER.exception("realtime outbox payload not serializable")
        return "{}"


def _load(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}
