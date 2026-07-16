"""SQLAlchemy-backed schedule repository."""

from __future__ import annotations

from datetime import date

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload, sessionmaker

from kokoro_link.contracts.schedule_repository import ScheduleRepositoryPort
from kokoro_link.domain.entities.schedule import DailySchedule
from kokoro_link.infrastructure.persistence.models import (
    DailyScheduleRow,
    ScheduleActivityRow,
)
from kokoro_link.infrastructure.persistence.sa_schedule_mapping import (
    apply_schedule_to_row,
    row_to_schedule,
    schedule_to_row,
)


class SAScheduleRepository(ScheduleRepositoryPort):
    def __init__(self, session_factory: sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get(self, character_id: str, date_: date) -> DailySchedule | None:
        async with self._session_factory() as session:
            stmt = (
                select(DailyScheduleRow)
                .where(
                    DailyScheduleRow.character_id == character_id,
                    DailyScheduleRow.date == date_.isoformat(),
                )
                .options(selectinload(DailyScheduleRow.activities))
            )
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            if row is None:
                return None
            return row_to_schedule(row)

    async def save(self, schedule: DailySchedule) -> None:
        async with self._session_factory() as session:
            stmt = (
                select(DailyScheduleRow)
                .where(DailyScheduleRow.id == schedule.id)
                .options(selectinload(DailyScheduleRow.activities))
            )
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            if row is None:
                # upsert by (character_id, date) — allows regenerate without
                # leaking old rows when a fresh schedule gets a new UUID.
                existing_stmt = (
                    select(DailyScheduleRow)
                    .where(
                        DailyScheduleRow.character_id == schedule.character_id,
                        DailyScheduleRow.date == schedule.date.isoformat(),
                    )
                    .options(selectinload(DailyScheduleRow.activities))
                )
                existing = (await session.execute(existing_stmt)).scalar_one_or_none()
                if existing is not None:
                    await session.delete(existing)
                    await session.flush()
                session.add(schedule_to_row(schedule))
            else:
                apply_schedule_to_row(schedule, row)
            await session.commit()

    async def list_for_character(
        self,
        character_id: str,
        *,
        limit: int = 30,
    ) -> list[DailySchedule]:
        async with self._session_factory() as session:
            stmt = (
                select(DailyScheduleRow)
                .where(DailyScheduleRow.character_id == character_id)
                .order_by(DailyScheduleRow.date.desc())
                .limit(limit)
                .options(selectinload(DailyScheduleRow.activities))
            )
            result = await session.execute(stmt)
            rows = list(result.scalars().all())
        return [row_to_schedule(row) for row in rows]

    async def delete_for_character(self, character_id: str) -> int:
        async with self._session_factory() as session:
            id_stmt = select(DailyScheduleRow.id).where(
                DailyScheduleRow.character_id == character_id
            )
            ids = [row for row in (await session.execute(id_stmt)).scalars().all()]
            if not ids:
                return 0
            # Delete activities first, then schedules.
            await session.execute(
                delete(ScheduleActivityRow).where(
                    ScheduleActivityRow.schedule_id.in_(ids)
                )
            )
            await session.execute(
                delete(DailyScheduleRow).where(DailyScheduleRow.id.in_(ids))
            )
            await session.commit()
            return len(ids)
