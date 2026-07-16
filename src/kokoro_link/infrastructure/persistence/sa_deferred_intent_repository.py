"""SQLAlchemy deferred-intent repository (HUMANIZATION_ROADMAP §3.4)."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker

from kokoro_link.contracts.deferred_intent import DeferredIntentRepositoryPort
from kokoro_link.domain.entities.deferred_intent import (
    STATUS_ACTIVE,
    STATUS_CONSUMED,
    STATUS_EXPIRED,
    DeferredIntent,
)
from kokoro_link.infrastructure.persistence.models import DeferredIntentRow


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def _row_to_domain(row: DeferredIntentRow) -> DeferredIntent:
    return DeferredIntent(
        id=row.id,
        character_id=row.character_id,
        operator_id=row.operator_id,
        trigger=row.trigger,
        inner_motive=row.inner_motive,
        conversation_purpose=row.conversation_purpose,
        expected_reply=row.expected_reply,
        risk=row.risk,
        best_timing=row.best_timing,
        reason=row.reason,
        status=row.status,
        created_at=_ensure_utc(row.created_at),
        expires_at=_ensure_utc(row.expires_at),
        consumed_at=_ensure_utc(row.consumed_at) if row.consumed_at else None,
    )


def _domain_to_row(intent: DeferredIntent) -> DeferredIntentRow:
    return DeferredIntentRow(
        id=intent.id,
        character_id=intent.character_id,
        operator_id=intent.operator_id,
        trigger=intent.trigger,
        inner_motive=intent.inner_motive,
        conversation_purpose=intent.conversation_purpose,
        expected_reply=intent.expected_reply,
        risk=intent.risk,
        best_timing=intent.best_timing,
        reason=intent.reason,
        status=intent.status,
        created_at=intent.created_at,
        expires_at=intent.expires_at,
        consumed_at=intent.consumed_at,
    )


class SADeferredIntentRepository(DeferredIntentRepositoryPort):
    def __init__(self, session_factory: sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def add(self, intent: DeferredIntent) -> DeferredIntent:
        async with self._session_factory() as session:
            session.add(_domain_to_row(intent))
            await session.commit()
        return intent

    async def list_active_for(
        self,
        character_id: str,
        operator_id: str,
        *,
        now: datetime,
        limit: int = 5,
    ) -> list[DeferredIntent]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(DeferredIntentRow)
                .where(
                    DeferredIntentRow.character_id == character_id,
                    DeferredIntentRow.operator_id == operator_id,
                    DeferredIntentRow.status == STATUS_ACTIVE,
                    DeferredIntentRow.expires_at > now,
                )
                .order_by(DeferredIntentRow.created_at.desc())
                .limit(max(1, limit)),
            )
            return [_row_to_domain(r) for r in result.scalars().all()]

    async def mark_consumed(
        self, intent_id: str, *, now: datetime,
    ) -> bool:
        async with self._session_factory() as session:
            result = await session.execute(
                update(DeferredIntentRow)
                .where(
                    DeferredIntentRow.id == intent_id,
                    DeferredIntentRow.status == STATUS_ACTIVE,
                )
                .values(status=STATUS_CONSUMED, consumed_at=now),
            )
            await session.commit()
            return bool(result.rowcount)

    async def gc_expired(self, *, now: datetime) -> int:
        async with self._session_factory() as session:
            result = await session.execute(
                update(DeferredIntentRow)
                .where(
                    DeferredIntentRow.status == STATUS_ACTIVE,
                    DeferredIntentRow.expires_at <= now,
                )
                .values(status=STATUS_EXPIRED),
            )
            await session.commit()
            return int(result.rowcount or 0)
