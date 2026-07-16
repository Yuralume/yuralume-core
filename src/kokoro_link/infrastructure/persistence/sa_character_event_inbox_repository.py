"""SQLAlchemy adapter for ``CharacterEventInboxRepositoryPort``.

The crucial method is ``claim`` — it must atomically transition a row
from unclaimed to claimed-by-``surface``. We do that with
``UPDATE ... WHERE claimed_by_surface IS NULL`` so the database (not
Python) decides which surface wins a race.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from kokoro_link.domain.entities.character_event_inbox import (
    CharacterEventInboxItem,
)
from kokoro_link.infrastructure.persistence.rss_models import (
    CharacterEventInboxRow,
)


def _ensure_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _row_to_domain(row: CharacterEventInboxRow) -> CharacterEventInboxItem:
    return CharacterEventInboxItem(
        id=row.id,
        character_id=row.character_id,
        world_event_id=row.world_event_id,
        similarity=float(row.similarity or 0.0),
        created_at=_ensure_utc(row.created_at) or datetime.now(timezone.utc),
        claimed_by_surface=row.claimed_by_surface,
        claimed_at=_ensure_utc(row.claimed_at),
    )


class SaCharacterEventInboxRepository:
    def __init__(self, session_factory: async_sessionmaker) -> None:
        self._session_factory = session_factory

    async def add_many(self, items: list[CharacterEventInboxItem]) -> None:
        if not items:
            return
        async with self._session_factory() as session:
            session.add_all(
                CharacterEventInboxRow(
                    id=i.id,
                    character_id=i.character_id,
                    world_event_id=i.world_event_id,
                    similarity=i.similarity,
                    created_at=i.created_at,
                    claimed_by_surface=i.claimed_by_surface,
                    claimed_at=i.claimed_at,
                )
                for i in items
            )
            try:
                await session.commit()
            except Exception:
                # Unique constraint on (character_id, world_event_id) —
                # caller should pre-check via has_event, but fail-soft
                # here to keep curator batch operations from aborting.
                await session.rollback()

    async def list_for_character(
        self,
        character_id: str,
        *,
        unclaimed_only: bool = False,
        surface: str | None = None,
        limit: int | None = None,
    ) -> list[CharacterEventInboxItem]:
        async with self._session_factory() as session:
            stmt = (
                select(CharacterEventInboxRow)
                .where(CharacterEventInboxRow.character_id == character_id)
                .order_by(CharacterEventInboxRow.created_at.asc())
            )
            if unclaimed_only:
                stmt = stmt.where(
                    CharacterEventInboxRow.claimed_by_surface.is_(None)
                )
            elif surface is not None:
                stmt = stmt.where(
                    CharacterEventInboxRow.claimed_by_surface == surface
                )
            if limit is not None:
                stmt = stmt.limit(limit)
            rows = (await session.execute(stmt)).scalars().all()
        return [_row_to_domain(r) for r in rows]

    async def claim(
        self, item_id: str, *, surface: str, at: datetime,
    ) -> CharacterEventInboxItem | None:
        async with self._session_factory() as session:
            result = await session.execute(
                update(CharacterEventInboxRow)
                .where(CharacterEventInboxRow.id == item_id)
                .where(CharacterEventInboxRow.claimed_by_surface.is_(None))
                .values(claimed_by_surface=surface, claimed_at=at)
                .returning(CharacterEventInboxRow.id)
            )
            won = result.scalar_one_or_none()
            await session.commit()
            if not won:
                return None
            row = (await session.execute(
                select(CharacterEventInboxRow).where(
                    CharacterEventInboxRow.id == item_id
                )
            )).scalar_one()
            return _row_to_domain(row)

    async def release(
        self, item_id: str, *, surface: str,
    ) -> bool:
        async with self._session_factory() as session:
            result = await session.execute(
                update(CharacterEventInboxRow)
                .where(CharacterEventInboxRow.id == item_id)
                .where(CharacterEventInboxRow.claimed_by_surface == surface)
                .values(claimed_by_surface=None, claimed_at=None)
                .returning(CharacterEventInboxRow.id)
            )
            won = result.scalar_one_or_none()
            await session.commit()
            return won is not None

    async def count_unclaimed(self, character_id: str) -> int:
        async with self._session_factory() as session:
            result = await session.execute(
                select(func.count())
                .select_from(CharacterEventInboxRow)
                .where(CharacterEventInboxRow.character_id == character_id)
                .where(CharacterEventInboxRow.claimed_by_surface.is_(None))
            )
            return int(result.scalar_one() or 0)

    async def trim_oldest(
        self, character_id: str, *, keep: int,
    ) -> int:
        if keep < 0:
            keep = 0
        async with self._session_factory() as session:
            id_stmt = (
                select(CharacterEventInboxRow.id)
                .where(CharacterEventInboxRow.character_id == character_id)
                .order_by(CharacterEventInboxRow.created_at.desc())
                .offset(keep)
            )
            ids_to_delete = (
                (await session.execute(id_stmt)).scalars().all()
            )
            if not ids_to_delete:
                return 0
            result = await session.execute(
                delete(CharacterEventInboxRow).where(
                    CharacterEventInboxRow.id.in_(ids_to_delete)
                )
            )
            await session.commit()
            return result.rowcount or 0

    async def delete_older_than(self, cutoff: datetime) -> int:
        async with self._session_factory() as session:
            result = await session.execute(
                delete(CharacterEventInboxRow).where(
                    CharacterEventInboxRow.created_at < cutoff
                )
            )
            await session.commit()
            return result.rowcount or 0

    async def delete_for_event(self, world_event_id: str) -> int:
        async with self._session_factory() as session:
            result = await session.execute(
                delete(CharacterEventInboxRow).where(
                    CharacterEventInboxRow.world_event_id == world_event_id
                )
            )
            await session.commit()
            return result.rowcount or 0

    async def has_event(
        self, character_id: str, world_event_id: str,
    ) -> bool:
        async with self._session_factory() as session:
            row = (await session.execute(
                select(CharacterEventInboxRow.id)
                .where(CharacterEventInboxRow.character_id == character_id)
                .where(CharacterEventInboxRow.world_event_id == world_event_id)
            )).scalar_one_or_none()
            return row is not None
