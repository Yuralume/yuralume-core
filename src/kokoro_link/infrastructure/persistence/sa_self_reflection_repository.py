"""SQLAlchemy self-reflection repository (HUMANIZATION_ROADMAP §3.2)."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker

from kokoro_link.contracts.self_reflection import (
    SelfReflectionRepositoryPort,
)
from kokoro_link.domain.entities.self_reflection import SelfReflection
from kokoro_link.infrastructure.persistence.models import SelfReflectionRow


def _ensure_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _decode_str_list(raw: str) -> list[str]:
    if not raw:
        return []
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return [s for s in decoded if isinstance(s, str) and s.strip()] if isinstance(decoded, list) else []


def _row_to_domain(row: SelfReflectionRow) -> SelfReflection:
    return SelfReflection(
        id=row.id,
        character_id=row.character_id,
        operator_id=row.operator_id,
        period=row.period,
        narrative=row.narrative,
        dominant_themes=tuple(_decode_str_list(row.dominant_themes)),
        period_start=row.period_start,
        period_end=row.period_end,
        evidence_quotes=tuple(_decode_str_list(row.evidence_quotes)),
        created_at=_ensure_utc(row.created_at),
    )


class SASelfReflectionRepository(SelfReflectionRepositoryPort):
    def __init__(self, session_factory: sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def upsert_latest(
        self, reflection: SelfReflection,
    ) -> SelfReflection:
        async with self._session_factory() as session:
            result = await session.execute(
                select(SelfReflectionRow).where(
                    SelfReflectionRow.character_id == reflection.character_id,
                    SelfReflectionRow.operator_id == reflection.operator_id,
                    SelfReflectionRow.period == reflection.period,
                ),
            )
            existing = result.scalars().first()
            themes_json = json.dumps(
                list(reflection.dominant_themes), ensure_ascii=False,
            )
            quotes_json = json.dumps(
                list(reflection.evidence_quotes), ensure_ascii=False,
            )
            if existing is None:
                row = SelfReflectionRow(
                    id=reflection.id,
                    character_id=reflection.character_id,
                    operator_id=reflection.operator_id,
                    period=reflection.period,
                    narrative=reflection.narrative,
                    dominant_themes=themes_json,
                    period_start=reflection.period_start,
                    period_end=reflection.period_end,
                    evidence_quotes=quotes_json,
                    created_at=reflection.created_at,
                )
                session.add(row)
            else:
                existing.narrative = reflection.narrative
                existing.dominant_themes = themes_json
                existing.period_start = reflection.period_start
                existing.period_end = reflection.period_end
                existing.evidence_quotes = quotes_json
                existing.created_at = reflection.created_at
            await session.commit()
            return reflection

    async def latest_for(
        self, character_id: str, operator_id: str,
    ) -> list[SelfReflection]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(SelfReflectionRow)
                .where(
                    SelfReflectionRow.character_id == character_id,
                    SelfReflectionRow.operator_id == operator_id,
                )
                .order_by(SelfReflectionRow.created_at.desc()),
            )
            return [_row_to_domain(r) for r in result.scalars().all()]

    async def delete_for_character(self, character_id: str) -> int:
        async with self._session_factory() as session:
            result = await session.execute(
                delete(SelfReflectionRow).where(
                    SelfReflectionRow.character_id == character_id,
                ),
            )
            await session.commit()
            return int(result.rowcount or 0)
