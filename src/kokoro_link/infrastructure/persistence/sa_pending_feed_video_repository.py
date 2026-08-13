"""SQLAlchemy repository for deferred LumeGram video posts (CV4)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker

from kokoro_link.contracts.clock import ensure_utc
from kokoro_link.contracts.feed_video_jobs import (
    PENDING_VIDEO_STATUS_PENDING,
    PENDING_VIDEO_TERMINAL_STATUSES,
    PendingFeedVideo,
)
from kokoro_link.infrastructure.persistence.models import PendingFeedVideoRow


class SAPendingFeedVideoRepository:
    def __init__(self, session_factory: sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def add(self, record: PendingFeedVideo) -> None:
        async with self._session_factory() as session:
            session.add(_to_row(record))
            await session.commit()

    async def get(self, record_id: str) -> PendingFeedVideo | None:
        async with self._session_factory() as session:
            row = await session.get(PendingFeedVideoRow, record_id)
            return _to_entity(row) if row is not None else None

    async def list_due(
        self, *, now: datetime, limit: int = 50,
    ) -> list[PendingFeedVideo]:
        stmt = (
            select(PendingFeedVideoRow)
            .where(
                PendingFeedVideoRow.status == PENDING_VIDEO_STATUS_PENDING,
                PendingFeedVideoRow.next_poll_at <= ensure_utc(now),
            )
            .order_by(PendingFeedVideoRow.next_poll_at.asc())
            .limit(max(1, int(limit)))
        )
        async with self._session_factory() as session:
            result = await session.execute(stmt)
            return [_to_entity(row) for row in result.scalars()]

    async def has_pending_for_character(self, character_id: str) -> bool:
        """``SELECT 1 ... LIMIT 1`` — existence, not the row.

        Runs on every feed tick of a deployment that can queue renders, so
        it reads one indexed column and stops at the first hit rather than
        hydrating a draft nobody is going to look at."""
        stmt = (
            select(PendingFeedVideoRow.id)
            .where(
                PendingFeedVideoRow.character_id == character_id,
                PendingFeedVideoRow.status == PENDING_VIDEO_STATUS_PENDING,
            )
            .limit(1)
        )
        async with self._session_factory() as session:
            result = await session.execute(stmt)
            return result.scalar_one_or_none() is not None

    async def reschedule(
        self, record: PendingFeedVideo, *, next_poll_at: datetime, now: datetime,
    ) -> bool:
        stmt = (
            update(PendingFeedVideoRow)
            .where(
                PendingFeedVideoRow.id == record.id,
                PendingFeedVideoRow.status == PENDING_VIDEO_STATUS_PENDING,
            )
            .values(
                next_poll_at=ensure_utc(next_poll_at),
                attempts=PendingFeedVideoRow.attempts + 1,
                updated_at=ensure_utc(now),
            )
        )
        async with self._session_factory() as session:
            result = await session.execute(stmt)
            await session.commit()
            return bool(result.rowcount)

    async def finish_if_pending(
        self,
        record_id: str,
        *,
        status: str,
        now: datetime,
        post_id: str | None = None,
        note: str = "",
    ) -> bool:
        """One UPDATE guarded on ``status = 'pending'``.

        The guard lives in the WHERE clause rather than a read-then-write
        so it stays atomic across replicas: the embedded sweep and a
        distributed worker can observe the same finished job at the same
        instant, and the database hands exactly one of them a non-zero
        ``rowcount`` — the one that is allowed to publish."""
        if status not in PENDING_VIDEO_TERMINAL_STATUSES:
            raise ValueError(
                f"finish_if_pending expects a terminal status, got {status!r}",
            )
        stmt = (
            update(PendingFeedVideoRow)
            .where(
                PendingFeedVideoRow.id == record_id,
                PendingFeedVideoRow.status == PENDING_VIDEO_STATUS_PENDING,
            )
            .values(
                status=status,
                post_id=post_id,
                note=(note or "")[:200],
                updated_at=ensure_utc(now),
            )
        )
        async with self._session_factory() as session:
            result = await session.execute(stmt)
            await session.commit()
            return bool(result.rowcount)

    async def attach_post(
        self, record_id: str, *, post_id: str, now: datetime,
    ) -> bool:
        stmt = (
            update(PendingFeedVideoRow)
            .where(PendingFeedVideoRow.id == record_id)
            .values(post_id=post_id, updated_at=ensure_utc(now))
        )
        async with self._session_factory() as session:
            result = await session.execute(stmt)
            await session.commit()
            return bool(result.rowcount)

    async def delete_finished_before(self, cutoff: datetime) -> int:
        stmt = delete(PendingFeedVideoRow).where(
            PendingFeedVideoRow.status != PENDING_VIDEO_STATUS_PENDING,
            PendingFeedVideoRow.updated_at < ensure_utc(cutoff),
        )
        async with self._session_factory() as session:
            result = await session.execute(stmt)
            await session.commit()
            return int(result.rowcount or 0)


def _to_row(record: PendingFeedVideo) -> PendingFeedVideoRow:
    return PendingFeedVideoRow(
        id=record.id,
        character_id=record.character_id,
        operator_id=record.operator_id,
        job_id=record.job_id,
        status=record.status,
        content_text=record.content_text,
        post_kind=record.post_kind,
        source_kind=record.source_kind,
        source_ref_id=record.source_ref_id,
        first_frame_url=record.first_frame_url,
        first_frame_key=record.first_frame_key,
        image_prompt=record.image_prompt,
        video_prompt=record.video_prompt,
        submitted_at=ensure_utc(record.submitted_at),
        timeout_seconds=record.timeout_seconds,
        next_poll_at=ensure_utc(record.next_poll_at),
        attempts=record.attempts,
        post_id=record.post_id,
        note=record.note,
        created_at=ensure_utc(record.created_at),
        updated_at=ensure_utc(record.updated_at),
    )


def _to_entity(row: PendingFeedVideoRow) -> PendingFeedVideo:
    return PendingFeedVideo(
        id=row.id,
        character_id=row.character_id,
        operator_id=row.operator_id or "",
        job_id=row.job_id,
        status=row.status,
        content_text=row.content_text,
        post_kind=row.post_kind,
        source_kind=row.source_kind,
        source_ref_id=row.source_ref_id,
        first_frame_url=row.first_frame_url,
        first_frame_key=row.first_frame_key or "",
        image_prompt=row.image_prompt or "",
        video_prompt=row.video_prompt or "",
        submitted_at=ensure_utc(row.submitted_at),
        timeout_seconds=row.timeout_seconds,
        next_poll_at=ensure_utc(row.next_poll_at),
        attempts=row.attempts,
        post_id=row.post_id,
        note=row.note or "",
        created_at=ensure_utc(row.created_at),
        updated_at=ensure_utc(row.updated_at),
    )
