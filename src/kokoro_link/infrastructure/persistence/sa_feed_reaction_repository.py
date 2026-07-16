"""SQLAlchemy-backed ``FeedReactionRepositoryPort`` implementation.

Mirrors ``SAFeedPostRepository`` in shape. Idempotency on ``add`` is
enforced via a SELECT-then-insert inside one session: the unique
constraint on (post_id, liker_id) makes a race between two concurrent
likes safe at the DB level (one insert wins, the other gets a
constraint violation we map back to the existing row by re-reading).
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker

from kokoro_link.contracts.feed import FeedReactionRepositoryPort
from kokoro_link.domain.entities.feed_reaction import FeedReaction
from kokoro_link.infrastructure.persistence.models import FeedReactionRow


def _ensure_utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _row_to_domain(row: FeedReactionRow) -> FeedReaction:
    return FeedReaction(
        id=row.id,
        post_id=row.post_id,
        liker_id=row.liker_id,
        created_at=_ensure_utc(row.created_at),
    )


class SAFeedReactionRepository(FeedReactionRepositoryPort):
    def __init__(self, session_factory: sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def add(self, reaction: FeedReaction) -> FeedReaction:
        async with self._session_factory() as session:
            existing = await self._fetch(
                session, post_id=reaction.post_id, liker_id=reaction.liker_id,
            )
            if existing is not None:
                return _row_to_domain(existing)
            row = FeedReactionRow(
                id=reaction.id,
                post_id=reaction.post_id,
                liker_id=reaction.liker_id,
                created_at=reaction.created_at,
            )
            session.add(row)
            try:
                await session.commit()
            except IntegrityError:
                # Race: another concurrent ``add`` won the insert. Roll
                # back, re-fetch, and return that row so the caller's
                # subsequent recount lands on a stable state.
                await session.rollback()
                existing = await self._fetch(
                    session,
                    post_id=reaction.post_id, liker_id=reaction.liker_id,
                )
                if existing is None:
                    raise
                return _row_to_domain(existing)
            return reaction

    async def remove(self, *, post_id: str, liker_id: str) -> bool:
        async with self._session_factory() as session:
            result = await session.execute(
                delete(FeedReactionRow).where(
                    FeedReactionRow.post_id == post_id,
                    FeedReactionRow.liker_id == liker_id,
                ),
            )
            await session.commit()
            return (result.rowcount or 0) > 0

    async def has_liked(self, *, post_id: str, liker_id: str) -> bool:
        async with self._session_factory() as session:
            existing = await self._fetch(
                session, post_id=post_id, liker_id=liker_id,
            )
            return existing is not None

    async def count_for_post(self, post_id: str) -> int:
        async with self._session_factory() as session:
            result = await session.execute(
                select(func.count(FeedReactionRow.id)).where(
                    FeedReactionRow.post_id == post_id,
                ),
            )
            return int(result.scalar() or 0)

    async def liked_post_ids(
        self, *, post_ids: tuple[str, ...], liker_id: str,
    ) -> set[str]:
        if not post_ids:
            return set()
        async with self._session_factory() as session:
            result = await session.execute(
                select(FeedReactionRow.post_id).where(
                    FeedReactionRow.liker_id == liker_id,
                    FeedReactionRow.post_id.in_(post_ids),
                ),
            )
            return {row[0] for row in result.all()}

    async def list_since(
        self, *, post_id: str, since: datetime | None,
    ) -> list[FeedReaction]:
        async with self._session_factory() as session:
            stmt = select(FeedReactionRow).where(
                FeedReactionRow.post_id == post_id,
            )
            if since is not None:
                stmt = stmt.where(FeedReactionRow.created_at > since)
            stmt = stmt.order_by(FeedReactionRow.created_at.asc())
            rows = (await session.execute(stmt)).scalars().all()
            return [_row_to_domain(r) for r in rows]

    async def _fetch(
        self,
        session: AsyncSession,
        *,
        post_id: str,
        liker_id: str,
    ) -> FeedReactionRow | None:
        result = await session.execute(
            select(FeedReactionRow).where(
                FeedReactionRow.post_id == post_id,
                FeedReactionRow.liker_id == liker_id,
            ),
        )
        return result.scalar_one_or_none()
