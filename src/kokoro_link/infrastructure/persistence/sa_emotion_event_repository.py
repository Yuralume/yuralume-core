"""SQLAlchemy emotion-event repository."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker

from kokoro_link.contracts.emotion import EmotionEventRepositoryPort
from kokoro_link.domain.entities.emotion_event import EmotionEvent
from kokoro_link.infrastructure.persistence.models import EmotionEventRow


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def _row_to_domain(row: EmotionEventRow) -> EmotionEvent:
    return EmotionEvent(
        id=row.id,
        character_id=row.character_id,
        operator_id=row.operator_id,
        cause_ref_kind=row.cause_ref_kind,
        cause_ref_id=row.cause_ref_id,
        valence=row.valence,
        arousal=row.arousal,
        intensity=row.intensity,
        affection_delta=row.affection_delta,
        fatigue_delta=row.fatigue_delta,
        trust_delta=row.trust_delta,
        energy_delta=row.energy_delta,
        applied_to_state=row.applied_to_state,
        emotion_label=row.emotion_label,
        evidence_quote=row.evidence_quote,
        decay_half_life_minutes=row.decay_half_life_minutes,
        expires_at=_ensure_utc(row.expires_at) if row.expires_at else None,
        created_at=_ensure_utc(row.created_at),
    )


def _domain_to_row(event: EmotionEvent) -> EmotionEventRow:
    return EmotionEventRow(
        id=event.id,
        character_id=event.character_id,
        operator_id=event.operator_id,
        cause_ref_kind=event.cause_ref_kind,
        cause_ref_id=event.cause_ref_id,
        valence=event.valence,
        arousal=event.arousal,
        intensity=event.intensity,
        affection_delta=event.affection_delta,
        fatigue_delta=event.fatigue_delta,
        trust_delta=event.trust_delta,
        energy_delta=event.energy_delta,
        applied_to_state=event.applied_to_state,
        emotion_label=event.emotion_label,
        evidence_quote=event.evidence_quote,
        decay_half_life_minutes=event.decay_half_life_minutes,
        expires_at=event.expires_at,
        created_at=event.created_at,
    )


class SAEmotionEventRepository(EmotionEventRepositoryPort):
    def __init__(self, session_factory: sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def add(self, event: EmotionEvent) -> None:
        async with self._session_factory() as session:
            session.add(_domain_to_row(event))
            await session.commit()

    async def add_many(self, events: list[EmotionEvent]) -> None:
        if not events:
            return
        async with self._session_factory() as session:
            for event in events:
                session.add(_domain_to_row(event))
            await session.commit()

    async def list_recent(
        self,
        *,
        character_id: str,
        operator_id: str,
        since: datetime,
        limit: int = 100,
    ) -> list[EmotionEvent]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(EmotionEventRow)
                .where(
                    EmotionEventRow.character_id == character_id,
                    EmotionEventRow.operator_id == operator_id,
                    EmotionEventRow.created_at >= since,
                )
                .order_by(EmotionEventRow.created_at.desc())
                .limit(limit),
            )
            return [_row_to_domain(r) for r in result.scalars().all()]

    async def delete_for_character(self, character_id: str) -> int:
        async with self._session_factory() as session:
            result = await session.execute(
                delete(EmotionEventRow).where(
                    EmotionEventRow.character_id == character_id,
                ),
            )
            await session.commit()
            return int(result.rowcount or 0)
