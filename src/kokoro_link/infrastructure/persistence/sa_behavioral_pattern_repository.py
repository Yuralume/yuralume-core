"""SQLAlchemy behavioural-pattern repository (HUMANIZATION_ROADMAP §3.3)."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker

from kokoro_link.contracts.behavioral_pattern import (
    BehavioralPatternRepositoryPort,
)
from kokoro_link.domain.entities.behavioral_pattern import BehavioralPattern
from kokoro_link.infrastructure.persistence.models import BehavioralPatternRow


def _ensure_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _row_to_domain(row: BehavioralPatternRow) -> BehavioralPattern:
    return BehavioralPattern(
        id=row.id,
        character_id=row.character_id,
        kind=row.kind,
        description=row.description,
        observed_count=row.observed_count,
        first_observed_at=_ensure_utc(row.first_observed_at),
        last_observed_at=_ensure_utc(row.last_observed_at),
        salience=row.salience,
    )


class SABehavioralPatternRepository(BehavioralPatternRepositoryPort):
    def __init__(self, session_factory: sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def upsert(self, pattern: BehavioralPattern) -> BehavioralPattern:
        async with self._session_factory() as session:
            result = await session.execute(
                select(BehavioralPatternRow).where(
                    BehavioralPatternRow.character_id == pattern.character_id,
                    BehavioralPatternRow.kind == pattern.kind,
                    BehavioralPatternRow.description == pattern.description,
                ),
            )
            existing = result.scalars().first()
            if existing is None:
                row = BehavioralPatternRow(
                    id=pattern.id,
                    character_id=pattern.character_id,
                    kind=pattern.kind,
                    description=pattern.description,
                    observed_count=pattern.observed_count,
                    first_observed_at=pattern.first_observed_at,
                    last_observed_at=pattern.last_observed_at,
                    salience=pattern.salience,
                )
                session.add(row)
                await session.commit()
                return pattern
            existing.observed_count = existing.observed_count + pattern.observed_count
            existing.last_observed_at = max(
                _ensure_utc(existing.last_observed_at),
                pattern.last_observed_at,
            )
            existing.salience = max(existing.salience, pattern.salience)
            await session.commit()
            return _row_to_domain(existing)

    async def list_for_character(
        self,
        character_id: str,
        *,
        kinds: tuple[str, ...] | None = None,
        limit: int = 12,
    ) -> list[BehavioralPattern]:
        async with self._session_factory() as session:
            stmt = select(BehavioralPatternRow).where(
                BehavioralPatternRow.character_id == character_id,
            )
            if kinds:
                stmt = stmt.where(BehavioralPatternRow.kind.in_(kinds))
            stmt = stmt.order_by(
                BehavioralPatternRow.observed_count.desc(),
                BehavioralPatternRow.last_observed_at.desc(),
            ).limit(max(1, limit))
            result = await session.execute(stmt)
            return [_row_to_domain(r) for r in result.scalars().all()]

    async def delete_for_character(self, character_id: str) -> int:
        async with self._session_factory() as session:
            result = await session.execute(
                delete(BehavioralPatternRow).where(
                    BehavioralPatternRow.character_id == character_id,
                ),
            )
            await session.commit()
            return int(result.rowcount or 0)
