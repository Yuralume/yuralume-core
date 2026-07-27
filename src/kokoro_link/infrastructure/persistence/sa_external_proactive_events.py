"""SQLAlchemy adapter for the external proactive pre-send ledger (LH4).

The durable side of DR-LH0-005. Session conventions mirror
``sa_external_chat_turn_repository.py``: short sessions, never a DB transaction
held across the (long) channel HTTP call. ``save_pre_send`` locks the existing
row ``FOR UPDATE`` so concurrent pre-send writers of the same ``event_id``
serialise: exactly one wins a fresh insert (enforced by the PK), the rest
observe the committed row and replay it (or raise on a hash conflict). The
``mark_*`` transitions are single conditional UPDATEs gated on
``state = 'pending'`` so a terminal-ish row is never re-opened.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker

from kokoro_link.contracts.clock import ensure_utc
from kokoro_link.contracts.external_proactive_ledger import (
    ExternalProactiveEvent,
    ExternalProactiveEventConflict,
    ExternalProactiveEventRepositoryPort,
    ExternalProactiveEventState,
)
from kokoro_link.infrastructure.persistence.models import (
    ExternalProactiveEventRow,
)

_Row = ExternalProactiveEventRow
_S = ExternalProactiveEventState
_PENDING = _S.PENDING.value

_SAVE_INSERT_RETRIES = 3


class SAExternalProactiveEventRepository(ExternalProactiveEventRepositoryPort):
    def __init__(self, session_factory: sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

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
        now = ensure_utc(now)
        expires_at = ensure_utc(expires_at)
        for _attempt in range(_SAVE_INSERT_RETRIES):
            async with self._session_factory() as session:
                row = (
                    await session.execute(
                        select(_Row)
                        .where(_Row.event_id == event_id)
                        .with_for_update(),
                    )
                ).scalar_one_or_none()
                if row is not None:
                    if row.payload_hash != payload_hash:
                        await session.rollback()
                        raise ExternalProactiveEventConflict(event_id)
                    existing = _to_event(row)
                    await session.commit()
                    return existing

                new_row = _Row(
                    event_id=event_id,
                    tenant_id=tenant_id,
                    account_id=account_id,
                    character_id=character_id,
                    kind=kind,
                    envelope_json=envelope_json,
                    payload_hash=payload_hash,
                    state=_PENDING,
                    accept_attempts=0,
                    last_error=None,
                    created_at=now,
                    updated_at=now,
                    expires_at=expires_at,
                )
                session.add(new_row)
                # Build the DTO BEFORE commit — ``expire_on_commit`` would evict
                # the attributes and re-reading them outside the async greenlet
                # raises MissingGreenlet. All values are Python-set here.
                saved = _to_event(new_row)
                try:
                    await session.commit()
                except IntegrityError:
                    # A concurrent writer inserted the same event_id first —
                    # resolve the existing row on the next pass.
                    await session.rollback()
                    continue
                return saved
        raise RuntimeError(
            "save_pre_send: exhausted insert retries against a racing writer",
        )

    async def mark_accepted(self, event_id: str, *, now: datetime) -> bool:
        return await self._transition(
            event_id,
            now=now,
            values={
                "state": _S.ACCEPTED.value,
                "accept_attempts": _Row.accept_attempts + 1,
            },
        )

    async def mark_terminal(
        self, event_id: str, *, reason: str, now: datetime,
    ) -> bool:
        return await self._transition(
            event_id,
            now=now,
            values={"state": _S.TERMINAL.value, "last_error": reason},
        )

    async def mark_expired(self, event_id: str, *, now: datetime) -> bool:
        return await self._transition(
            event_id,
            now=now,
            values={"state": _S.EXPIRED.value},
        )

    async def list_pending_due(
        self, *, now: datetime, limit: int = 100,
    ) -> list[ExternalProactiveEvent]:
        now = ensure_utc(now)
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(_Row)
                    .where(
                        _Row.state == _PENDING,
                        _Row.expires_at > now,
                    )
                    .order_by(_Row.created_at.asc(), _Row.event_id.asc())
                    .limit(max(0, limit)),
                )
            ).scalars().all()
            return [_to_event(row) for row in rows]

    async def mark_conversation_recorded(
        self, event_id: str, *, now: datetime,
    ) -> bool:
        async with self._session_factory() as session:
            result = await session.execute(
                update(_Row)
                .where(
                    _Row.event_id == event_id,
                    _Row.state == _S.ACCEPTED.value,
                    _Row.conversation_recorded.is_(False),
                )
                .values(conversation_recorded=True, updated_at=ensure_utc(now)),
            )
            await session.commit()
            return result.rowcount == 1

    async def list_accepted_unrecorded(
        self, *, now: datetime, limit: int = 100,
    ) -> list[ExternalProactiveEvent]:
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(_Row)
                    .where(
                        _Row.state == _S.ACCEPTED.value,
                        _Row.conversation_recorded.is_(False),
                    )
                    .order_by(_Row.created_at.asc(), _Row.event_id.asc())
                    .limit(max(0, limit)),
                )
            ).scalars().all()
            return [_to_event(row) for row in rows]

    async def get(self, event_id: str) -> ExternalProactiveEvent | None:
        async with self._session_factory() as session:
            row = await session.get(_Row, event_id)
            return _to_event(row) if row is not None else None

    async def _transition(
        self,
        event_id: str,
        *,
        now: datetime,
        values: dict[str, object],
    ) -> bool:
        payload = dict(values)
        payload["updated_at"] = ensure_utc(now)
        async with self._session_factory() as session:
            result = await session.execute(
                update(_Row)
                .where(_Row.event_id == event_id, _Row.state == _PENDING)
                .values(**payload),
            )
            await session.commit()
            return result.rowcount == 1


def _to_event(row: ExternalProactiveEventRow) -> ExternalProactiveEvent:
    return ExternalProactiveEvent(
        event_id=row.event_id,
        tenant_id=row.tenant_id,
        account_id=row.account_id,
        character_id=row.character_id,
        kind=row.kind,
        envelope_json=row.envelope_json,
        payload_hash=row.payload_hash,
        state=ExternalProactiveEventState(row.state),
        accept_attempts=row.accept_attempts,
        last_error=row.last_error,
        created_at=ensure_utc(row.created_at),
        updated_at=ensure_utc(row.updated_at),
        expires_at=ensure_utc(row.expires_at),
        conversation_recorded=bool(row.conversation_recorded),
    )
