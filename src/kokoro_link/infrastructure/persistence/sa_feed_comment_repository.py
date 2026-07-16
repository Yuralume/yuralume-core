"""SQLAlchemy-backed ``FeedCommentRepositoryPort`` implementation.

Mirrors ``SAFeedReactionRepository`` in shape minus the idempotency
dance — comments are never deduped, so ``add`` is a plain INSERT.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker

from kokoro_link.contracts.feed import FeedCommentRepositoryPort
from kokoro_link.domain.entities.feed_comment import FeedComment
from kokoro_link.infrastructure.persistence.models import FeedCommentRow


def _ensure_utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _row_to_domain(row: FeedCommentRow) -> FeedComment:
    return FeedComment(
        id=row.id,
        post_id=row.post_id,
        author_id=row.author_id,
        content_text=row.content_text,
        created_at=_ensure_utc(row.created_at),
    )


class SAFeedCommentRepository(FeedCommentRepositoryPort):
    def __init__(self, session_factory: sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def add(self, comment: FeedComment) -> FeedComment:
        async with self._session_factory() as session:
            row = FeedCommentRow(
                id=comment.id,
                post_id=comment.post_id,
                author_id=comment.author_id,
                content_text=comment.content_text,
                created_at=comment.created_at,
            )
            session.add(row)
            await session.commit()
            return comment

    async def get(self, comment_id: str) -> FeedComment | None:
        async with self._session_factory() as session:
            result = await session.execute(
                select(FeedCommentRow).where(FeedCommentRow.id == comment_id),
            )
            row = result.scalar_one_or_none()
            return _row_to_domain(row) if row is not None else None

    async def remove(self, comment_id: str) -> bool:
        async with self._session_factory() as session:
            result = await session.execute(
                delete(FeedCommentRow).where(FeedCommentRow.id == comment_id),
            )
            await session.commit()
            return (result.rowcount or 0) > 0

    async def list_for_post(
        self,
        post_id: str,
        *,
        limit: int = 50,
        before: datetime | None = None,
    ) -> list[FeedComment]:
        clamped = max(1, min(limit, 200))
        async with self._session_factory() as session:
            stmt = select(FeedCommentRow).where(
                FeedCommentRow.post_id == post_id,
            )
            if before is not None:
                stmt = stmt.where(FeedCommentRow.created_at < before)
            stmt = stmt.order_by(
                FeedCommentRow.created_at.desc(),
            ).limit(clamped)
            rows = (await session.execute(stmt)).scalars().all()
            return [_row_to_domain(r) for r in rows]

    async def count_for_post(self, post_id: str) -> int:
        async with self._session_factory() as session:
            result = await session.execute(
                select(func.count(FeedCommentRow.id)).where(
                    FeedCommentRow.post_id == post_id,
                ),
            )
            return int(result.scalar() or 0)

    async def list_since(
        self, *, post_id: str, since: datetime | None,
    ) -> list[FeedComment]:
        async with self._session_factory() as session:
            stmt = select(FeedCommentRow).where(
                FeedCommentRow.post_id == post_id,
            )
            if since is not None:
                stmt = stmt.where(FeedCommentRow.created_at > since)
            stmt = stmt.order_by(FeedCommentRow.created_at.asc())
            rows = (await session.execute(stmt)).scalars().all()
            return [_row_to_domain(r) for r in rows]
