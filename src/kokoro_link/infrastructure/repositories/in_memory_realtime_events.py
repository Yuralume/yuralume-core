"""In-memory realtime-event outbox — behaviour parity for the unit suite.

Mirrors :class:`SARealtimeOutbox` semantics (append id assignment, the
out-of-order-safe ``read_after`` overlap window anchored to the cursor row's
``created_at``, and prune) without a database, so the unit tests can prove the
anti-loss contract offline. See ``contracts/realtime_events.py``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Mapping

from kokoro_link.contracts.clock import ensure_utc
from kokoro_link.contracts.realtime_events import (
    DEFAULT_OVERLAP,
    RealtimeEventSpec,
    StoredRealtimeEvent,
)


_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class _EventRecord:
    id: int
    event_kind: str
    payload: dict[str, Any]
    created_at: datetime
    tenant_id: str | None
    operator_id: str | None


class InMemoryRealtimeOutbox:
    """Process-local realtime-event outbox with parity read semantics."""

    def __init__(self) -> None:
        self._rows: list[_EventRecord] = []
        self._next_id: int = 1

    async def append(
        self, session: object, spec: RealtimeEventSpec,
    ) -> StoredRealtimeEvent:
        # ``session`` accepted for port parity; the in-memory append is
        # immediate (no shared transaction to ride).
        record = _EventRecord(
            id=self._next_id,
            event_kind=spec.event_kind,
            payload=dict(spec.payload),
            created_at=ensure_utc(spec.created_at),
            tenant_id=spec.tenant_id,
            operator_id=spec.operator_id,
        )
        self._next_id += 1
        self._rows.append(record)
        return _to_event(record)

    async def read_after(
        self,
        cursor_id: int,
        *,
        overlap: timedelta = DEFAULT_OVERLAP,
        limit: int,
    ) -> list[StoredRealtimeEvent]:
        ordered = sorted(self._rows, key=lambda r: r.id)
        if cursor_id <= 0:
            return [_to_event(r) for r in ordered[:limit]]

        anchor = next((r for r in ordered if r.id == cursor_id), None)
        if anchor is None:
            # Cursor row pruned: forward frontier only (parity with SA).
            forward = [r for r in ordered if r.id > cursor_id]
            return [_to_event(r) for r in forward[:limit]]

        threshold = anchor.created_at - overlap
        # Parity with the SA adapter: read the frontier and the overlap tail as
        # two bounded sets, NOT one ``id > cursor OR created_at >= threshold``
        # slice truncated by ``limit`` (which sorts the overlap tail ahead of the
        # frontier and starves it once the window holds ``>= limit`` rows). The
        # frontier keeps its own ``limit`` so the cursor always advances; the
        # overlap tail is bounded to the ``limit`` ids nearest the cursor.
        frontier = [r for r in ordered if r.id > cursor_id][:limit]
        overlap_tail = [
            r for r in ordered
            if r.id <= cursor_id and r.created_at >= threshold
        ][-limit:]
        return [_to_event(r) for r in overlap_tail + frontier]

    async def latest_id(self) -> int:
        return max((r.id for r in self._rows), default=0)

    async def notify_hint(self, *, event_id: int | None = None) -> None:
        # No cross-process channel in-process; nothing to wake.
        return None

    async def prune(self, older_than: datetime) -> int:
        cutoff = ensure_utc(older_than)
        before = len(self._rows)
        self._rows = [r for r in self._rows if r.created_at >= cutoff]
        return before - len(self._rows)


def _to_event(record: _EventRecord) -> StoredRealtimeEvent:
    return StoredRealtimeEvent(
        id=record.id,
        event_kind=record.event_kind,
        payload=dict(record.payload),
        created_at=record.created_at,
        tenant_id=record.tenant_id,
        operator_id=record.operator_id,
    )
