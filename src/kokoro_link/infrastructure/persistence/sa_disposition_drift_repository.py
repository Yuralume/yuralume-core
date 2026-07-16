"""SQLAlchemy disposition-drift audit repository (HUMANIZATION_ROADMAP §3.1)."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker

from kokoro_link.contracts.disposition_drift import (
    DispositionDriftHistoryRepositoryPort,
)
from kokoro_link.domain.entities.disposition_drift_record import (
    DispositionDriftRecord,
)
from kokoro_link.infrastructure.persistence.models import (
    DispositionDriftHistoryRow,
)


def _ensure_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _row_to_domain(row: DispositionDriftHistoryRow) -> DispositionDriftRecord:
    return DispositionDriftRecord(
        id=row.id,
        character_id=row.character_id,
        dimension=row.dimension,
        from_band=row.from_band,
        to_band=row.to_band,
        reason=row.reason,
        evidence_quote=row.evidence_quote,
        decided_at=_ensure_utc(row.decided_at),
    )


class SADispositionDriftHistoryRepository(
    DispositionDriftHistoryRepositoryPort,
):
    def __init__(self, session_factory: sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def add(
        self, record: DispositionDriftRecord,
    ) -> DispositionDriftRecord:
        async with self._session_factory() as session:
            session.add(DispositionDriftHistoryRow(
                id=record.id,
                character_id=record.character_id,
                dimension=record.dimension,
                from_band=record.from_band,
                to_band=record.to_band,
                reason=record.reason,
                evidence_quote=record.evidence_quote,
                decided_at=record.decided_at,
            ))
            await session.commit()
        return record

    async def list_for_character(
        self, character_id: str, *, limit: int = 20,
    ) -> list[DispositionDriftRecord]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(DispositionDriftHistoryRow)
                .where(DispositionDriftHistoryRow.character_id == character_id)
                .order_by(DispositionDriftHistoryRow.decided_at.desc())
                .limit(max(1, limit)),
            )
            return [_row_to_domain(r) for r in result.scalars().all()]

    async def latest_for_dimension(
        self, character_id: str, dimension: str,
    ) -> DispositionDriftRecord | None:
        async with self._session_factory() as session:
            result = await session.execute(
                select(DispositionDriftHistoryRow)
                .where(
                    DispositionDriftHistoryRow.character_id == character_id,
                    DispositionDriftHistoryRow.dimension == dimension,
                )
                .order_by(DispositionDriftHistoryRow.decided_at.desc())
                .limit(1),
            )
            row = result.scalars().first()
            return _row_to_domain(row) if row is not None else None

    async def delete_for_character(self, character_id: str) -> int:
        async with self._session_factory() as session:
            result = await session.execute(
                delete(DispositionDriftHistoryRow).where(
                    DispositionDriftHistoryRow.character_id == character_id,
                ),
            )
            await session.commit()
            return int(result.rowcount or 0)
