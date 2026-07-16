"""SQLAlchemy-backed ``StateHistoryRepositoryPort`` implementation."""

from datetime import timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker

from kokoro_link.contracts.state_history import StateHistoryRepositoryPort
from kokoro_link.domain.entities.state_snapshot import StateSnapshot
from kokoro_link.infrastructure.persistence.models import StateSnapshotRow


class SAStateHistoryRepository(StateHistoryRepositoryPort):
    def __init__(self, session_factory: sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def add(self, snapshot: StateSnapshot) -> None:
        async with self._session_factory() as session:
            session.add(_to_row(snapshot))
            await session.commit()

    async def query(
        self,
        character_id: str,
        *,
        limit: int = 50,
    ) -> list[StateSnapshot]:
        async with self._session_factory() as session:
            stmt = (
                select(StateSnapshotRow)
                .where(StateSnapshotRow.character_id == character_id)
                .order_by(StateSnapshotRow.created_at.desc())
                .limit(limit)
            )
            result = await session.execute(stmt)
            rows = list(result.scalars().all())
        return [_to_domain(row) for row in rows]

    async def delete_many(self, snapshot_ids: list[str]) -> int:
        if not snapshot_ids:
            return 0
        async with self._session_factory() as session:
            result = await session.execute(
                delete(StateSnapshotRow).where(
                    StateSnapshotRow.id.in_(snapshot_ids),
                )
            )
            await session.commit()
            return result.rowcount or 0

    async def delete_created_since(
        self, character_id: str, since,
    ) -> int:
        async with self._session_factory() as session:
            result = await session.execute(
                delete(StateSnapshotRow).where(
                    StateSnapshotRow.character_id == character_id,
                    StateSnapshotRow.created_at >= since,
                )
            )
            await session.commit()
            return result.rowcount or 0

    async def delete_for_character(self, character_id: str) -> int:
        async with self._session_factory() as session:
            count_stmt = select(StateSnapshotRow.id).where(
                StateSnapshotRow.character_id == character_id,
            )
            count = len(list((await session.execute(count_stmt)).scalars().all()))
            if count == 0:
                return 0
            await session.execute(
                delete(StateSnapshotRow).where(
                    StateSnapshotRow.character_id == character_id,
                )
            )
            await session.commit()
            return count


def _to_row(snapshot: StateSnapshot) -> StateSnapshotRow:
    return StateSnapshotRow(
        id=snapshot.id,
        character_id=snapshot.character_id,
        source=snapshot.source,
        emotion=snapshot.emotion,
        affection=snapshot.affection,
        fatigue=snapshot.fatigue,
        trust=snapshot.trust,
        energy=snapshot.energy,
        created_at=snapshot.created_at,
        trigger=snapshot.trigger,
    )


def _to_domain(row: StateSnapshotRow) -> StateSnapshot:
    created_at = row.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return StateSnapshot(
        id=row.id,
        character_id=row.character_id,
        source=row.source,
        emotion=row.emotion,
        affection=row.affection,
        fatigue=row.fatigue,
        trust=row.trust,
        energy=row.energy,
        created_at=created_at,
        trigger=row.trigger,
    )
