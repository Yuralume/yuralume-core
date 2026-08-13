"""SQLAlchemy adapter for ``CharacterEventMentionRepositoryPort``.

The write is an upsert on ``(character_id, world_event_id)``: raising the
same event a second time is the same relationship with a fresher
timestamp. Dialect is resolved per session the same way
``sa_inbound_receipts`` does it, because self-host runs SQLite and hosted
runs PostgreSQL and both need the same conflict clause.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import async_sessionmaker

from kokoro_link.domain.entities.character_event_mention import (
    CharacterEventMention,
)
from kokoro_link.infrastructure.persistence.rss_models import (
    CharacterEventMentionRow,
)

_CONFLICT_COLUMNS = (
    CharacterEventMentionRow.character_id,
    CharacterEventMentionRow.world_event_id,
)


def _ensure_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _row_to_domain(row: CharacterEventMentionRow) -> CharacterEventMention:
    return CharacterEventMention(
        id=row.id,
        character_id=row.character_id,
        world_event_id=row.world_event_id,
        surface=row.surface,
        mentioned_at=(
            _ensure_utc(row.mentioned_at) or datetime.now(timezone.utc)
        ),
    )


class SaCharacterEventMentionRepository:
    def __init__(self, session_factory: async_sessionmaker) -> None:
        self._session_factory = session_factory

    async def record(self, mention: CharacterEventMention) -> None:
        async with self._session_factory() as session:
            bind = session.bind
            insert = (
                pg_insert
                if bind is not None and bind.dialect.name == "postgresql"
                else sqlite_insert
            )
            statement = (
                insert(CharacterEventMentionRow)
                .values(
                    id=mention.id,
                    character_id=mention.character_id,
                    world_event_id=mention.world_event_id,
                    surface=mention.surface,
                    mentioned_at=mention.mentioned_at,
                )
                .on_conflict_do_update(
                    index_elements=list(_CONFLICT_COLUMNS),
                    set_={
                        "surface": mention.surface,
                        "mentioned_at": mention.mentioned_at,
                    },
                )
            )
            await session.execute(statement)
            await session.commit()

    async def list_recent(
        self, character_id: str, *, limit: int = 5,
    ) -> list[CharacterEventMention]:
        if limit <= 0:
            return []
        async with self._session_factory() as session:
            statement = (
                select(CharacterEventMentionRow)
                .where(CharacterEventMentionRow.character_id == character_id)
                .order_by(CharacterEventMentionRow.mentioned_at.desc())
                .limit(limit)
            )
            rows = (await session.execute(statement)).scalars().all()
            return [_row_to_domain(row) for row in rows]

    async def delete_for_character(self, character_id: str) -> int:
        async with self._session_factory() as session:
            result = await session.execute(
                delete(CharacterEventMentionRow).where(
                    CharacterEventMentionRow.character_id == character_id,
                ),
            )
            await session.commit()
            return int(result.rowcount or 0)

    async def delete_older_than(self, cutoff: datetime) -> int:
        async with self._session_factory() as session:
            result = await session.execute(
                delete(CharacterEventMentionRow).where(
                    CharacterEventMentionRow.mentioned_at < cutoff,
                ),
            )
            await session.commit()
            return int(result.rowcount or 0)
