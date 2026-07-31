"""SQLAlchemy-backed schedule repository."""

from __future__ import annotations

from datetime import date

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload, sessionmaker

from kokoro_link.contracts.schedule_repository import ScheduleRepositoryPort
from kokoro_link.domain.entities.schedule import DailySchedule
from kokoro_link.infrastructure.persistence.models import (
    DailyScheduleRow,
    ScheduleActivityRow,
)


# Only a violation of THIS constraint (concurrent upsert on the same civil
# day) is the benign race-recovery path; any other integrity failure is a
# real defect and must surface.
_SCHEDULE_DATE_CONSTRAINT = "uq_daily_schedules_character_date"


def _is_schedule_date_violation(error: IntegrityError) -> bool:
    return _SCHEDULE_DATE_CONSTRAINT in str(getattr(error, "orig", error))


# A concurrent (character, date) insert is resolved on the retry (re-read the
# winner, replace in place). Two passes suffice for a two-writer race; the cap
# bounds any pathological ping-pong.
_UPSERT_MAX_ATTEMPTS = 3
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
        # Upsert on (character_id, date). Single-runner behaviour is unchanged
        # (the happy path never raises); the ``uq_daily_schedules_character_date``
        # constraint added by P3-Dedup only bites when a concurrent runner
        # inserts the same civil day between our read and commit. We recover
        # from THAT race idempotently (re-read the winner, replace in place)
        # so exactly one row survives instead of a 500 — a strictly-safer
        # hardening of the pre-existing read-delete-insert upsert.
        for attempt in range(_UPSERT_MAX_ATTEMPTS):
            async with self._session_factory() as session:
                stmt = (
                    select(DailyScheduleRow)
                    .where(DailyScheduleRow.id == schedule.id)
                    .options(selectinload(DailyScheduleRow.activities))
                )
                result = await session.execute(stmt)
                row = result.scalar_one_or_none()
                if row is not None:
                    apply_schedule_to_row(schedule, row)
                    await session.commit()
                    return
                # No row by id — upsert by (character_id, date) so a regenerate
                # with a fresh UUID replaces the day rather than leaking a dupe.
                existing_stmt = (
                    select(DailyScheduleRow)
                    .where(
                        DailyScheduleRow.character_id == schedule.character_id,
                        DailyScheduleRow.date == schedule.date.isoformat(),
                    )
                    .options(selectinload(DailyScheduleRow.activities))
                )
                existing = (
                    await session.execute(existing_stmt)
                ).scalar_one_or_none()
                if existing is not None:
                    await session.delete(existing)
                    await session.flush()
                session.add(schedule_to_row(schedule))
                try:
                    await session.commit()
                    return
                except IntegrityError as exc:
                    # A concurrent writer inserted this (character, date) after
                    # our read. Any other integrity failure is a real defect.
                    await session.rollback()
                    if not _is_schedule_date_violation(exc):
                        raise
                    if attempt == _UPSERT_MAX_ATTEMPTS - 1:
                        raise
                    # Retry: the next pass re-reads the winner and replaces it.
                    continue

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

    async def set_weather_vet(
        self,
        character_id: str,
        date_: date,
        *,
        activity_id: str | None,
        condition: str | None,
    ) -> None:
        # Two columns, one statement — the activity rows are never touched, so
        # a memorialize claim / encounter block / chat commitment that landed
        # while the drift judge was thinking survives untouched. Blank halves
        # normalise the same way ``DailySchedule.with_weather_vet`` does so the
        # gate reads identically whichever path wrote it.
        async with self._session_factory() as session:
            await session.execute(
                update(DailyScheduleRow)
                .where(
                    DailyScheduleRow.character_id == character_id,
                    DailyScheduleRow.date == date_.isoformat(),
                )
                .values(
                    weather_vet_activity_id=(activity_id or "").strip() or None,
                    weather_vet_condition=(condition or "").strip() or None,
                ),
            )
            await session.commit()

    async def claim_memorialize(self, activity_ids: list[str]) -> set[str]:
        if not activity_ids:
            return set()
        async with self._session_factory() as session:
            # RETURNING id tells us exactly which rows THIS caller flipped
            # false → true — the atomic claim that gates the memory write so a
            # racing runner cannot double-write the episodic memory.
            result = await session.execute(
                update(ScheduleActivityRow)
                .where(
                    ScheduleActivityRow.id.in_(activity_ids),
                    ScheduleActivityRow.memorialized.is_(False),
                )
                .values(memorialized=True)
                .returning(ScheduleActivityRow.id),
            )
            claimed = {row_id for (row_id,) in result.all()}
            await session.commit()
            return claimed

    async def release_memorialize(self, activity_ids: list[str]) -> None:
        if not activity_ids:
            return
        async with self._session_factory() as session:
            await session.execute(
                update(ScheduleActivityRow)
                .where(ScheduleActivityRow.id.in_(activity_ids))
                .values(memorialized=False),
            )
            await session.commit()
