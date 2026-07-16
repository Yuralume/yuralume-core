"""SQLAlchemy adapter for ``RssSourceRepositoryPort``."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import async_sessionmaker

from kokoro_link.domain.entities.rss_source import RssSource
from kokoro_link.infrastructure.persistence.rss_models import RssSourceRow


def _ensure_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _row_to_domain(row: RssSourceRow) -> RssSource:
    return RssSource(
        id=row.id,
        name=row.name,
        feed_url=row.feed_url,
        category=row.category or "news",
        locale=row.locale or "zh-TW",
        enabled=bool(row.enabled),
        last_attempt_at=_ensure_utc(row.last_attempt_at),
        last_success_at=_ensure_utc(row.last_success_at),
        last_error=row.last_error,
        fetched_count_total=int(row.fetched_count_total or 0),
        default_for_categories=tuple(
            json.loads(row.default_for_categories or "[]")
        ),
    )


class SaRssSourceRepository:
    def __init__(self, session_factory: async_sessionmaker) -> None:
        self._session_factory = session_factory

    async def list_all(self, *, enabled_only: bool = False) -> list[RssSource]:
        async with self._session_factory() as session:
            stmt = select(RssSourceRow).order_by(RssSourceRow.id.asc())
            if enabled_only:
                stmt = stmt.where(RssSourceRow.enabled.is_(True))
            rows = (await session.execute(stmt)).scalars().all()
        return [_row_to_domain(r) for r in rows]

    async def get(self, source_id: str) -> RssSource | None:
        async with self._session_factory() as session:
            row = (await session.execute(
                select(RssSourceRow).where(RssSourceRow.id == source_id)
            )).scalar_one_or_none()
        return _row_to_domain(row) if row else None

    async def upsert(self, source: RssSource) -> None:
        payload = {
            "id": source.id,
            "name": source.name,
            "feed_url": source.feed_url,
            "category": source.category,
            "locale": source.locale,
            "enabled": source.enabled,
            "last_attempt_at": source.last_attempt_at,
            "last_success_at": source.last_success_at,
            "last_error": source.last_error,
            "fetched_count_total": source.fetched_count_total,
            "default_for_categories": json.dumps(
                list(source.default_for_categories), ensure_ascii=False,
            ),
        }
        async with self._session_factory() as session:
            stmt = pg_insert(RssSourceRow).values(**payload)
            stmt = stmt.on_conflict_do_update(
                index_elements=[RssSourceRow.id],
                set_={
                    k: stmt.excluded[k] for k in payload
                    if k != "id"
                },
            )
            await session.execute(stmt)
            await session.commit()

    async def delete(self, source_id: str) -> None:
        async with self._session_factory() as session:
            await session.execute(
                delete(RssSourceRow).where(RssSourceRow.id == source_id)
            )
            await session.commit()

    async def mark_success(
        self, source_id: str, *, at: datetime, fetched_count: int,
    ) -> None:
        async with self._session_factory() as session:
            await session.execute(
                update(RssSourceRow)
                .where(RssSourceRow.id == source_id)
                .values(
                    last_attempt_at=at,
                    last_success_at=at,
                    last_error=None,
                    fetched_count_total=(
                        RssSourceRow.fetched_count_total
                        + max(0, int(fetched_count))
                    ),
                )
            )
            await session.commit()

    async def mark_error(
        self, source_id: str, *, at: datetime, error: str,
    ) -> None:
        async with self._session_factory() as session:
            await session.execute(
                update(RssSourceRow)
                .where(RssSourceRow.id == source_id)
                .values(
                    last_attempt_at=at,
                    last_error=(error or "unknown error")[:500],
                )
            )
            await session.commit()
