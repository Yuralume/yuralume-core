"""In-memory external proactive pre-send ledger — first-class test citizen.

Full-fidelity twin of :class:`SAExternalProactiveEventRepository`: same pre-send
idempotency, the same hash-conflict rule, the same sticky terminal-ish states
and the same pending-due window — so the shared contract suite exercises the
real ledger without a database.

See ``contracts/external_proactive_ledger.py``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime

from kokoro_link.contracts.clock import ensure_utc
from kokoro_link.contracts.external_proactive_ledger import (
    ExternalProactiveEvent,
    ExternalProactiveEventConflict,
    ExternalProactiveEventRepositoryPort,
    ExternalProactiveEventState,
)

_S = ExternalProactiveEventState


@dataclass(slots=True)
class _EventRecord:
    """Mutable in-memory row (converted to a frozen view on read)."""

    event_id: str
    tenant_id: str
    account_id: str
    character_id: str
    kind: str
    envelope_json: str
    payload_hash: str
    state: ExternalProactiveEventState
    accept_attempts: int
    last_error: str | None
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
    conversation_recorded: bool = False


class InMemoryExternalProactiveEventRepository(
    ExternalProactiveEventRepositoryPort,
):
    def __init__(self) -> None:
        self._records: dict[str, _EventRecord] = {}
        self._mutex = asyncio.Lock()

    async def save_pre_send(
        self,
        *,
        event_id: str,
        tenant_id: str,
        account_id: str,
        character_id: str,
        kind: str,
        envelope_json: str,
        payload_hash: str,
        expires_at: datetime,
        now: datetime,
    ) -> ExternalProactiveEvent:
        async with self._mutex:
            existing = self._records.get(event_id)
            if existing is not None:
                if existing.payload_hash != payload_hash:
                    raise ExternalProactiveEventConflict(event_id)
                return _to_event(existing)
            now = ensure_utc(now)
            record = _EventRecord(
                event_id=event_id,
                tenant_id=tenant_id,
                account_id=account_id,
                character_id=character_id,
                kind=kind,
                envelope_json=envelope_json,
                payload_hash=payload_hash,
                state=_S.PENDING,
                accept_attempts=0,
                last_error=None,
                created_at=now,
                updated_at=now,
                expires_at=ensure_utc(expires_at),
            )
            self._records[event_id] = record
            return _to_event(record)

    async def mark_accepted(self, event_id: str, *, now: datetime) -> bool:
        async with self._mutex:
            record = self._pending(event_id)
            if record is None:
                return False
            record.state = _S.ACCEPTED
            record.accept_attempts += 1
            record.updated_at = ensure_utc(now)
            return True

    async def mark_terminal(
        self, event_id: str, *, reason: str, now: datetime,
    ) -> bool:
        async with self._mutex:
            record = self._pending(event_id)
            if record is None:
                return False
            record.state = _S.TERMINAL
            record.last_error = reason
            record.updated_at = ensure_utc(now)
            return True

    async def mark_expired(self, event_id: str, *, now: datetime) -> bool:
        async with self._mutex:
            record = self._pending(event_id)
            if record is None:
                return False
            record.state = _S.EXPIRED
            record.updated_at = ensure_utc(now)
            return True

    async def list_pending_due(
        self, *, now: datetime, limit: int = 100,
    ) -> list[ExternalProactiveEvent]:
        now = ensure_utc(now)
        due = [
            record
            for record in self._records.values()
            if record.state is _S.PENDING
            and ensure_utc(record.expires_at) > now
        ]
        due.sort(key=lambda record: (ensure_utc(record.created_at), record.event_id))
        return [_to_event(record) for record in due[: max(0, limit)]]

    async def mark_conversation_recorded(
        self, event_id: str, *, now: datetime,
    ) -> bool:
        async with self._mutex:
            record = self._records.get(event_id)
            if (
                record is None
                or record.state is not _S.ACCEPTED
                or record.conversation_recorded
            ):
                return False
            record.conversation_recorded = True
            record.updated_at = ensure_utc(now)
            return True

    async def list_accepted_unrecorded(
        self, *, now: datetime, limit: int = 100,
    ) -> list[ExternalProactiveEvent]:
        unrecorded = [
            record
            for record in self._records.values()
            if record.state is _S.ACCEPTED and not record.conversation_recorded
        ]
        unrecorded.sort(
            key=lambda record: (ensure_utc(record.created_at), record.event_id),
        )
        return [_to_event(record) for record in unrecorded[: max(0, limit)]]

    async def get(self, event_id: str) -> ExternalProactiveEvent | None:
        record = self._records.get(event_id)
        return _to_event(record) if record is not None else None

    def _pending(self, event_id: str) -> _EventRecord | None:
        record = self._records.get(event_id)
        if record is None or record.state is not _S.PENDING:
            return None
        return record


def _to_event(record: _EventRecord) -> ExternalProactiveEvent:
    return ExternalProactiveEvent(
        event_id=record.event_id,
        tenant_id=record.tenant_id,
        account_id=record.account_id,
        character_id=record.character_id,
        kind=record.kind,
        envelope_json=record.envelope_json,
        payload_hash=record.payload_hash,
        state=record.state,
        accept_attempts=record.accept_attempts,
        last_error=record.last_error,
        created_at=record.created_at,
        updated_at=record.updated_at,
        expires_at=record.expires_at,
        conversation_recorded=record.conversation_recorded,
    )
