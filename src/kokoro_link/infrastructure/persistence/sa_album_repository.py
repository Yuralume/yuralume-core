"""SQLAlchemy-backed ``AlbumRepositoryPort`` implementation."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker

from kokoro_link.contracts.album import AlbumRepositoryPort
from kokoro_link.domain.entities.album_item import AlbumItem
from kokoro_link.infrastructure.persistence.models import CharacterAlbumItemRow


def _ensure_utc(value: datetime) -> datetime:
    """asyncpg returns tz-aware; mapping layer defends against sync
    drivers too so tests that bypass asyncpg don't blow up downstream
    tz-aware invariants in the domain entity."""
    if value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def _row_to_domain(row: CharacterAlbumItemRow) -> AlbumItem:
    return AlbumItem(
        id=row.id,
        character_id=row.character_id,
        url=row.url,
        source=row.source,
        caption=row.caption,
        byte_size=row.byte_size,
        created_at=_ensure_utc(row.created_at),
    )


def _domain_to_row(item: AlbumItem, row: CharacterAlbumItemRow) -> None:
    row.id = item.id
    row.character_id = item.character_id
    row.url = item.url
    row.source = item.source
    row.caption = item.caption
    row.byte_size = item.byte_size
    row.created_at = item.created_at


class SAAlbumRepository(AlbumRepositoryPort):
    def __init__(self, session_factory: sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def add(self, item: AlbumItem) -> None:
        async with self._session_factory() as session:
            row = CharacterAlbumItemRow(id=item.id)
            _domain_to_row(item, row)
            session.add(row)
            await session.commit()

    async def get(self, item_id: str) -> AlbumItem | None:
        async with self._session_factory() as session:
            row = await session.get(CharacterAlbumItemRow, item_id)
        return _row_to_domain(row) if row else None

    async def list_for_character(
        self,
        character_id: str,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[AlbumItem]:
        async with self._session_factory() as session:
            stmt = (
                select(CharacterAlbumItemRow)
                .where(CharacterAlbumItemRow.character_id == character_id)
                .order_by(CharacterAlbumItemRow.created_at.desc())
                .offset(offset)
            )
            if limit is not None:
                stmt = stmt.limit(limit)
            result = await session.execute(stmt)
            rows = list(result.scalars().all())
        return [_row_to_domain(row) for row in rows]

    async def count_for_character(self, character_id: str) -> int:
        async with self._session_factory() as session:
            stmt = select(func.count(CharacterAlbumItemRow.id)).where(
                CharacterAlbumItemRow.character_id == character_id,
            )
            result = await session.execute(stmt)
            return int(result.scalar_one())

    async def delete(self, item_id: str) -> bool:
        async with self._session_factory() as session:
            row = await session.get(CharacterAlbumItemRow, item_id)
            if row is None:
                return False
            await session.delete(row)
            await session.commit()
            return True

    async def delete_for_character(self, character_id: str) -> int:
        async with self._session_factory() as session:
            existing = await session.execute(
                select(func.count(CharacterAlbumItemRow.id)).where(
                    CharacterAlbumItemRow.character_id == character_id,
                ),
            )
            count = int(existing.scalar_one())
            if count == 0:
                return 0
            await session.execute(
                delete(CharacterAlbumItemRow).where(
                    CharacterAlbumItemRow.character_id == character_id,
                ),
            )
            await session.commit()
            return count
