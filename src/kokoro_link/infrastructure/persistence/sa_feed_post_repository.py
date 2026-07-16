"""SQLAlchemy-backed ``FeedPostRepositoryPort`` implementation.

Mirrors ``SAAlbumRepository``'s shape — single-table, no nested
aggregate to coordinate. The mapping layer is defensive about
``created_at`` tz-awareness for the same reason the album repo is.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime, time, timezone, tzinfo

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker

from kokoro_link.contracts.feed import FeedPostRepositoryPort
from kokoro_link.domain.entities.feed_post import FeedPost, FeedReactionSummary
from kokoro_link.domain.value_objects.feed_kind import FeedKind
from kokoro_link.domain.value_objects.feed_source import FeedSource
from kokoro_link.infrastructure.persistence.models import FeedPostRow


def _ensure_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def _row_to_domain(row: FeedPostRow) -> FeedPost:
    return FeedPost(
        id=row.id,
        character_id=row.character_id,
        kind=FeedKind.from_string(row.kind or "daily"),
        content_text=row.content_text,
        source=FeedSource(
            kind=row.source_kind or "manual",
            ref_id=row.source_ref_id,
        ),
        image_url=row.image_url,
        image_prompt=row.image_prompt,
        video_url=row.video_url,
        video_prompt=row.video_prompt,
        reactions=FeedReactionSummary(
            likes=int(row.likes_count or 0),
            comments=int(row.comments_count or 0),
        ),
        reactions_seen_at=_ensure_utc(row.reactions_seen_at),
        created_at=_ensure_utc(row.created_at) or datetime.now(timezone.utc),
    )


def _domain_to_row(post: FeedPost, row: FeedPostRow) -> None:
    row.id = post.id
    row.character_id = post.character_id
    row.kind = post.kind.value
    row.content_text = post.content_text
    row.source_kind = post.source.kind
    row.source_ref_id = post.source.ref_id
    row.image_url = post.image_url
    row.image_prompt = post.image_prompt
    row.video_url = post.video_url
    row.video_prompt = post.video_prompt
    row.likes_count = max(0, int(post.reactions.likes))
    row.comments_count = max(0, int(post.reactions.comments))
    row.reactions_seen_at = post.reactions_seen_at
    row.created_at = post.created_at


def _utc_day_bounds(
    on: date,
    *,
    local_tz: tzinfo = timezone.utc,
) -> tuple[datetime, datetime]:
    start = datetime.combine(on, time.min, tzinfo=local_tz).astimezone(timezone.utc)
    end = datetime.combine(on, time.max, tzinfo=local_tz).astimezone(timezone.utc)
    return start, end


class SAFeedPostRepository(FeedPostRepositoryPort):
    def __init__(self, session_factory: sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def add(self, post: FeedPost) -> None:
        async with self._session_factory() as session:
            row = FeedPostRow(id=post.id)
            _domain_to_row(post, row)
            session.add(row)
            await session.commit()

    async def get(self, post_id: str) -> FeedPost | None:
        async with self._session_factory() as session:
            row = await session.get(FeedPostRow, post_id)
        return _row_to_domain(row) if row else None

    async def list_for_character(
        self,
        character_id: str,
        *,
        limit: int = 20,
        before: datetime | None = None,
    ) -> list[FeedPost]:
        clamped = max(1, min(limit, 100))
        async with self._session_factory() as session:
            stmt = (
                select(FeedPostRow)
                .where(FeedPostRow.character_id == character_id)
                .order_by(FeedPostRow.created_at.desc())
                .limit(clamped)
            )
            if before is not None:
                stmt = stmt.where(FeedPostRow.created_at < before)
            result = await session.execute(stmt)
            rows = list(result.scalars().all())
        return [_row_to_domain(row) for row in rows]

    async def list_recent(
        self,
        *,
        limit: int = 20,
        before: datetime | None = None,
        character_ids: "Iterable[str] | None" = None,
    ) -> list[FeedPost]:
        clamped = max(1, min(limit, 100))
        if character_ids is not None:
            allowed = tuple(character_ids)
            if not allowed:
                return []
        else:
            allowed = None
        async with self._session_factory() as session:
            stmt = (
                select(FeedPostRow)
                .order_by(FeedPostRow.created_at.desc())
                .limit(clamped)
            )
            if before is not None:
                stmt = stmt.where(FeedPostRow.created_at < before)
            if allowed is not None:
                stmt = stmt.where(FeedPostRow.character_id.in_(allowed))
            result = await session.execute(stmt)
            rows = list(result.scalars().all())
        return [_row_to_domain(row) for row in rows]

    async def count_since(
        self,
        *,
        since: datetime,
        character_ids: "Iterable[str] | None" = None,
    ) -> int:
        if character_ids is not None:
            allowed = tuple(character_ids)
            if not allowed:
                return 0
        else:
            allowed = None
        async with self._session_factory() as session:
            stmt = select(func.count(FeedPostRow.id)).where(
                FeedPostRow.created_at > since,
            )
            if allowed is not None:
                stmt = stmt.where(FeedPostRow.character_id.in_(allowed))
            result = await session.execute(stmt)
            return int(result.scalar_one())

    async def count_on_date(
        self,
        character_id: str,
        *,
        on: date,
        local_tz: tzinfo = timezone.utc,
    ) -> int:
        start, end = _utc_day_bounds(on, local_tz=local_tz)
        async with self._session_factory() as session:
            stmt = select(func.count(FeedPostRow.id)).where(
                FeedPostRow.character_id == character_id,
                FeedPostRow.created_at >= start,
                FeedPostRow.created_at <= end,
            )
            result = await session.execute(stmt)
            return int(result.scalar_one())

    async def latest_for_character(
        self, character_id: str,
    ) -> FeedPost | None:
        async with self._session_factory() as session:
            stmt = (
                select(FeedPostRow)
                .where(FeedPostRow.character_id == character_id)
                .order_by(FeedPostRow.created_at.desc())
                .limit(1)
            )
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
        return _row_to_domain(row) if row else None

    async def find_by_source(
        self, character_id: str, source: FeedSource,
    ) -> FeedPost | None:
        async with self._session_factory() as session:
            stmt = (
                select(FeedPostRow)
                .where(FeedPostRow.character_id == character_id)
                .where(FeedPostRow.source_kind == source.kind)
            )
            if source.ref_id is None:
                stmt = stmt.where(FeedPostRow.source_ref_id.is_(None))
            else:
                stmt = stmt.where(FeedPostRow.source_ref_id == source.ref_id)
            stmt = stmt.order_by(FeedPostRow.created_at.desc()).limit(1)
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
        return _row_to_domain(row) if row else None

    async def save(self, post: FeedPost) -> None:
        async with self._session_factory() as session:
            row = await session.get(FeedPostRow, post.id)
            if row is None:
                raise ValueError(f"feed post {post.id!r} not found")
            _domain_to_row(post, row)
            await session.commit()

    async def delete(self, post_id: str) -> bool:
        async with self._session_factory() as session:
            row = await session.get(FeedPostRow, post_id)
            if row is None:
                return False
            await session.delete(row)
            await session.commit()
            return True

    async def delete_for_character(self, character_id: str) -> int:
        async with self._session_factory() as session:
            existing = await session.execute(
                select(func.count(FeedPostRow.id)).where(
                    FeedPostRow.character_id == character_id,
                ),
            )
            count = int(existing.scalar_one())
            if count == 0:
                return 0
            await session.execute(
                delete(FeedPostRow).where(
                    FeedPostRow.character_id == character_id,
                ),
            )
            await session.commit()
            return count
