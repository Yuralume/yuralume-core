"""SQLAlchemy adapter for ``WorldEventRepositoryPort``."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import async_sessionmaker

from kokoro_link.domain.entities.world_event import WorldEvent
from kokoro_link.infrastructure.persistence.rss_models import WorldEventRow


def _row_to_domain(row: WorldEventRow) -> WorldEvent:
    embedding = list(row.embedding) if row.embedding is not None else None
    tags_raw = row.topic_tags or "[]"
    return WorldEvent(
        id=row.id,
        source=row.source,
        title=row.title,
        summary=row.summary or "",
        url=row.url,
        published_at=_ensure_utc(row.published_at),
        fetched_at=_ensure_utc(row.fetched_at),
        category=row.category or "news",
        locale=row.locale or None,
        topic_tags=tuple(json.loads(tags_raw)),
        embedding=embedding,
    )


def _ensure_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class SaWorldEventRepository:
    def __init__(self, session_factory: async_sessionmaker) -> None:
        self._session_factory = session_factory

    async def query_recent(
        self,
        *,
        limit: int,
        topic_tags: list[str] | None = None,
        max_age_days: int | None = None,
    ) -> list[WorldEvent]:
        async with self._session_factory() as session:
            stmt = select(WorldEventRow).order_by(
                WorldEventRow.published_at.desc(),
            )
            if max_age_days is not None:
                cutoff = datetime.now(timezone.utc).replace(
                    microsecond=0,
                ) - _days(max_age_days)
                stmt = stmt.where(WorldEventRow.published_at >= cutoff)
            stmt = stmt.limit(limit * 4 if topic_tags else limit)
            rows = (await session.execute(stmt)).scalars().all()

        events = [_row_to_domain(r) for r in rows]
        if topic_tags:
            tag_set = {t.strip().lower() for t in topic_tags if t.strip()}
            filtered = [
                e for e in events
                if any(t.lower() in tag_set for t in e.topic_tags)
            ]
            return filtered[:limit] if filtered else events[:limit]
        return events[:limit]

    async def upsert(self, event: WorldEvent) -> None:
        payload = {
            "id": event.id,
            "source": event.source,
            "title": event.title,
            "summary": event.summary or "",
            "url": event.url,
            "published_at": event.published_at,
            "fetched_at": event.fetched_at,
            "category": event.category or "news",
            "locale": event.locale or None,
            "topic_tags": json.dumps(list(event.topic_tags), ensure_ascii=False),
            "embedding": event.embedding,
        }
        async with self._session_factory() as session:
            stmt = pg_insert(WorldEventRow).values(**payload)
            stmt = stmt.on_conflict_do_update(
                index_elements=[WorldEventRow.id],
                set_={
                    "title": stmt.excluded.title,
                    "summary": stmt.excluded.summary,
                    "category": stmt.excluded.category,
                    "locale": stmt.excluded.locale,
                    "topic_tags": stmt.excluded.topic_tags,
                    "embedding": stmt.excluded.embedding,
                    "fetched_at": stmt.excluded.fetched_at,
                },
            )
            await session.execute(stmt)
            await session.commit()

    async def delete_older_than(self, cutoff: datetime) -> int:
        async with self._session_factory() as session:
            result = await session.execute(
                delete(WorldEventRow).where(
                    WorldEventRow.fetched_at < cutoff,
                )
            )
            await session.commit()
            return result.rowcount or 0

    async def get(self, event_id: str) -> WorldEvent | None:
        async with self._session_factory() as session:
            row = (await session.execute(
                select(WorldEventRow).where(WorldEventRow.id == event_id)
            )).scalar_one_or_none()
            return _row_to_domain(row) if row else None

    async def has_url(self, url: str) -> bool:
        async with self._session_factory() as session:
            row = (await session.execute(
                select(WorldEventRow.id).where(WorldEventRow.url == url)
            )).scalar_one_or_none()
            return row is not None

    async def list_with_embeddings_in_window(
        self,
        *,
        since: datetime,
        categories: list[str] | None = None,
        limit: int = 500,
    ) -> list[WorldEvent]:
        async with self._session_factory() as session:
            stmt = (
                select(WorldEventRow)
                .where(WorldEventRow.published_at >= since)
                .where(WorldEventRow.embedding.is_not(None))
                .order_by(WorldEventRow.published_at.desc())
                .limit(limit)
            )
            if categories:
                normalised = [c.strip().lower() for c in categories if c.strip()]
                if normalised:
                    stmt = stmt.where(WorldEventRow.category.in_(normalised))
            rows = (await session.execute(stmt)).scalars().all()
        return [_row_to_domain(r) for r in rows]


def _days(n: int):
    from datetime import timedelta
    return timedelta(days=n)
