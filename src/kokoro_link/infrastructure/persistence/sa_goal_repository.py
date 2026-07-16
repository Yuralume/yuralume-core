"""SQLAlchemy-backed goal repository."""

import json
from datetime import timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker

from kokoro_link.contracts.goal_repository import GoalRepositoryPort
from kokoro_link.domain.entities.character_goal import CharacterGoal
from kokoro_link.domain.value_objects.goal_status import GoalStatus
from kokoro_link.infrastructure.persistence.models import CharacterGoalRow


class SAGoalRepository(GoalRepositoryPort):
    def __init__(self, session_factory: sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def add(self, goal: CharacterGoal) -> None:
        async with self._session_factory() as session:
            session.add(_to_row(goal))
            await session.commit()

    async def add_many(self, goals: list[CharacterGoal]) -> None:
        if not goals:
            return
        async with self._session_factory() as session:
            session.add_all([_to_row(g) for g in goals])
            await session.commit()

    async def get(self, goal_id: str) -> CharacterGoal | None:
        async with self._session_factory() as session:
            row = await session.get(CharacterGoalRow, goal_id)
            if row is None:
                return None
            return _to_domain(row)

    async def list_for_character(
        self,
        character_id: str,
        *,
        statuses: tuple[GoalStatus, ...] | None = None,
    ) -> list[CharacterGoal]:
        async with self._session_factory() as session:
            stmt = select(CharacterGoalRow).where(
                CharacterGoalRow.character_id == character_id
            )
            if statuses is not None:
                stmt = stmt.where(CharacterGoalRow.status.in_([s.value for s in statuses]))
            stmt = stmt.order_by(
                CharacterGoalRow.priority.desc(),
                CharacterGoalRow.created_at.asc(),
            )
            result = await session.execute(stmt)
            rows = list(result.scalars().all())
        return [_to_domain(row) for row in rows]

    async def save(self, goal: CharacterGoal) -> None:
        async with self._session_factory() as session:
            row = await session.get(CharacterGoalRow, goal.id)
            if row is None:
                row = _to_row(goal)
                session.add(row)
            else:
                _apply_to_row(goal, row)
            await session.commit()

    async def delete(self, goal_id: str) -> bool:
        async with self._session_factory() as session:
            row = await session.get(CharacterGoalRow, goal_id)
            if row is None:
                return False
            await session.delete(row)
            await session.commit()
            return True

    async def delete_for_character(self, character_id: str) -> int:
        async with self._session_factory() as session:
            count_stmt = select(CharacterGoalRow.id).where(
                CharacterGoalRow.character_id == character_id,
            )
            count = len(list((await session.execute(count_stmt)).scalars().all()))
            if count == 0:
                return 0
            await session.execute(
                delete(CharacterGoalRow).where(
                    CharacterGoalRow.character_id == character_id,
                )
            )
            await session.commit()
            return count


def _to_row(goal: CharacterGoal) -> CharacterGoalRow:
    row = CharacterGoalRow(id=goal.id)
    _apply_to_row(goal, row)
    return row


def _apply_to_row(goal: CharacterGoal, row: CharacterGoalRow) -> None:
    row.character_id = goal.character_id
    row.content = goal.content
    row.status = goal.status.value
    row.priority = goal.priority
    row.origin = goal.origin
    row.tags = json.dumps(list(goal.tags), ensure_ascii=False)
    row.created_at = goal.created_at
    row.last_progressed_at = goal.last_progressed_at
    row.review_notes = goal.review_notes


def _to_domain(row: CharacterGoalRow) -> CharacterGoal:
    created_at = row.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    progressed = row.last_progressed_at
    if progressed is not None and progressed.tzinfo is None:
        progressed = progressed.replace(tzinfo=timezone.utc)
    tags_raw = row.tags or "[]"
    return CharacterGoal(
        id=row.id,
        character_id=row.character_id,
        content=row.content,
        status=GoalStatus.from_string(row.status),
        priority=row.priority,
        origin=row.origin,
        tags=tuple(json.loads(tags_raw)),
        created_at=created_at,
        last_progressed_at=progressed,
        review_notes=row.review_notes,
    )
