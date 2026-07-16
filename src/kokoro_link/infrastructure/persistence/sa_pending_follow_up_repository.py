"""SQLAlchemy adapter for ``PendingFollowUpRepositoryPort``."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import asc, delete, desc, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from kokoro_link.contracts.pending_follow_up import (
    PendingFollowUpRepositoryPort,
)
from kokoro_link.domain.entities.pending_follow_up import (
    PendingFollowUp,
    PendingFollowUpKind,
    PendingFollowUpMessage,
    PendingFollowUpStatus,
)
from kokoro_link.domain.entities.conversation import MessageContentMode
from kokoro_link.infrastructure.persistence.models import PendingFollowUpRow

_OPEN_STATUSES: frozenset[str] = frozenset({
    PendingFollowUpStatus.QUEUED.value,
    PendingFollowUpStatus.RESOLVING.value,
})


class SaPendingFollowUpRepository(PendingFollowUpRepositoryPort):
    def __init__(self, session_factory: async_sessionmaker) -> None:
        self._session_factory = session_factory

    async def add(self, follow_up: PendingFollowUp) -> None:
        await self._upsert(follow_up)

    async def save(self, follow_up: PendingFollowUp) -> None:
        await self._upsert(follow_up)

    async def get(self, follow_up_id: str) -> PendingFollowUp | None:
        async with self._session_factory() as session:
            row = await session.get(PendingFollowUpRow, follow_up_id)
            return _row_to_domain(row) if row else None

    async def find_open_for_conversation(
        self, conversation_id: str,
    ) -> PendingFollowUp | None:
        async with self._session_factory() as session:
            stmt = (
                select(PendingFollowUpRow)
                .where(PendingFollowUpRow.conversation_id == conversation_id)
                .where(PendingFollowUpRow.status.in_(_OPEN_STATUSES))
                .order_by(desc(PendingFollowUpRow.queued_at))
                .limit(1)
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
            return _row_to_domain(row) if row else None

    async def list_due(
        self,
        *,
        now: datetime,
        limit: int = 50,
    ) -> list[PendingFollowUp]:
        async with self._session_factory() as session:
            stmt = (
                select(PendingFollowUpRow)
                .where(
                    PendingFollowUpRow.status
                    == PendingFollowUpStatus.QUEUED.value,
                )
                .where(PendingFollowUpRow.scheduled_for <= now)
                .order_by(asc(PendingFollowUpRow.scheduled_for))
                .limit(max(0, limit))
            )
            rows = (await session.execute(stmt)).scalars().all()
            return [_row_to_domain(r) for r in rows]

    async def list_open_for_character(
        self, character_id: str,
    ) -> list[PendingFollowUp]:
        async with self._session_factory() as session:
            stmt = (
                select(PendingFollowUpRow)
                .where(PendingFollowUpRow.character_id == character_id)
                .where(PendingFollowUpRow.status.in_(_OPEN_STATUSES))
                .order_by(asc(PendingFollowUpRow.queued_at))
            )
            rows = (await session.execute(stmt)).scalars().all()
            return [_row_to_domain(r) for r in rows]

    async def delete_for_conversation(self, conversation_id: str) -> int:
        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                delete(PendingFollowUpRow).where(
                    PendingFollowUpRow.conversation_id == conversation_id,
                ),
            )
            return result.rowcount or 0

    async def delete_for_character(self, character_id: str) -> int:
        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                delete(PendingFollowUpRow).where(
                    PendingFollowUpRow.character_id == character_id,
                ),
            )
            return result.rowcount or 0

    async def _upsert(self, follow_up: PendingFollowUp) -> None:
        async with self._session_factory() as session, session.begin():
            existing = await session.get(PendingFollowUpRow, follow_up.id)
            messages_payload = json.dumps(
                [_message_to_payload(m) for m in follow_up.messages],
                ensure_ascii=False,
            )
            if existing is None:
                session.add(PendingFollowUpRow(
                    id=follow_up.id,
                    character_id=follow_up.character_id,
                    conversation_id=follow_up.conversation_id,
                    status=follow_up.status.value,
                    activity_id=follow_up.activity_id,
                    brief_reply=follow_up.brief_reply,
                    defer_reason=follow_up.defer_reason,
                    messages_json=messages_payload,
                    scheduled_for=follow_up.scheduled_for,
                    queued_at=follow_up.queued_at,
                    updated_at=follow_up.updated_at,
                    resolved_at=follow_up.resolved_at,
                    resolved_message=follow_up.resolved_message,
                    last_error=follow_up.last_error,
                    kind=follow_up.kind.value,
                    promise_intent=follow_up.promise_intent,
                ))
            else:
                existing.character_id = follow_up.character_id
                existing.conversation_id = follow_up.conversation_id
                existing.status = follow_up.status.value
                existing.activity_id = follow_up.activity_id
                existing.brief_reply = follow_up.brief_reply
                existing.defer_reason = follow_up.defer_reason
                existing.messages_json = messages_payload
                existing.scheduled_for = follow_up.scheduled_for
                existing.queued_at = follow_up.queued_at
                existing.updated_at = follow_up.updated_at
                existing.resolved_at = follow_up.resolved_at
                existing.resolved_message = follow_up.resolved_message
                existing.last_error = follow_up.last_error
                existing.kind = follow_up.kind.value
                existing.promise_intent = follow_up.promise_intent


def _message_to_payload(message: PendingFollowUpMessage) -> dict:
    payload: dict[str, str] = {
        "content": message.content,
        "queued_at": message.queued_at.isoformat(),
        "content_mode": message.content_mode.value,
    }
    if message.safe_summary:
        payload["safe_summary"] = message.safe_summary
    if message.message_id:
        payload["message_id"] = message.message_id
    return payload


def _payload_to_message(payload: dict) -> PendingFollowUpMessage:
    raw = payload.get("queued_at")
    queued_at = (
        datetime.fromisoformat(raw) if raw
        else datetime.now(timezone.utc)
    )
    if queued_at.tzinfo is None:
        queued_at = queued_at.replace(tzinfo=timezone.utc)
    return PendingFollowUpMessage(
        content=str(payload.get("content") or ""),
        queued_at=queued_at,
        content_mode=_coerce_content_mode(payload.get("content_mode")),
        safe_summary=str(payload.get("safe_summary") or ""),
        message_id=payload.get("message_id"),
    )


def _coerce_content_mode(raw: object) -> MessageContentMode:
    try:
        return MessageContentMode(str(raw or "").strip().lower())
    except ValueError:
        return MessageContentMode.NORMAL


def _row_to_domain(row: PendingFollowUpRow) -> PendingFollowUp:
    messages_raw = json.loads(row.messages_json or "[]")
    messages = tuple(
        _payload_to_message(item) for item in messages_raw if isinstance(item, dict)
    )
    # ``kind`` and ``promise_intent`` may be absent on legacy rows that
    # predate migration ``bz5d7e20050`` (column default backfills them).
    kind_raw = getattr(row, "kind", None) or PendingFollowUpKind.BUSY_DEFER.value
    return PendingFollowUp(
        id=row.id,
        character_id=row.character_id,
        conversation_id=row.conversation_id,
        status=PendingFollowUpStatus(row.status),
        messages=messages,
        brief_reply=row.brief_reply,
        defer_reason=row.defer_reason or "",
        activity_id=row.activity_id,
        scheduled_for=_aware(row.scheduled_for),
        queued_at=_aware(row.queued_at),
        updated_at=_aware(row.updated_at),
        resolved_at=_aware_opt(row.resolved_at),
        resolved_message=row.resolved_message,
        last_error=row.last_error,
        kind=PendingFollowUpKind(kind_raw),
        promise_intent=getattr(row, "promise_intent", "") or "",
    )


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _aware_opt(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return _aware(value)
