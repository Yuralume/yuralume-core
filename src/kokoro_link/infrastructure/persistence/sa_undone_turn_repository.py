"""SQLAlchemy adapter for ``UndoneTurnRepositoryPort``."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from kokoro_link.contracts.undone_turn import UndoneTurnRepositoryPort
from kokoro_link.domain.entities.undone_turn import UndoneTurn
from kokoro_link.infrastructure.persistence.models import UndoneTurnRow


class SaUndoneTurnRepository(UndoneTurnRepositoryPort):
    def __init__(self, session_factory: async_sessionmaker) -> None:
        self._session_factory = session_factory

    async def record(self, tombstone: UndoneTurn) -> None:
        async with self._session_factory() as session, session.begin():
            existing = await session.get(
                UndoneTurnRow, tombstone.turn_record_id,
            )
            if existing is not None:
                # First write wins — see the in-memory adapter: bumping
                # ``undone_at`` on a repeat would extend the row's life
                # past the GC window it was already measured against.
                return
            session.add(UndoneTurnRow(
                turn_record_id=tombstone.turn_record_id,
                conversation_id=tombstone.conversation_id,
                undone_at=tombstone.undone_at,
            ))

    async def is_undone(self, turn_record_id: str) -> bool:
        async with self._session_factory() as session:
            stmt = (
                select(UndoneTurnRow.turn_record_id)
                .where(UndoneTurnRow.turn_record_id == turn_record_id)
                .limit(1)
            )
            return (await session.execute(stmt)).scalar_one_or_none() is not None

    async def prune(self, *, older_than: datetime) -> int:
        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                delete(UndoneTurnRow).where(
                    UndoneTurnRow.undone_at < _as_utc(older_than),
                ),
            )
            return result.rowcount or 0

    async def delete_for_conversation(self, conversation_id: str) -> int:
        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                delete(UndoneTurnRow).where(
                    UndoneTurnRow.conversation_id == conversation_id,
                ),
            )
            return result.rowcount or 0


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value
