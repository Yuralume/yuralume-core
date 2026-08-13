"""In-process ``PendingFeedVideoRepositoryPort`` for tests and dev mode."""

from __future__ import annotations

import threading
from dataclasses import replace
from datetime import datetime

from kokoro_link.contracts.clock import ensure_utc
from kokoro_link.contracts.feed_video_jobs import (
    PENDING_VIDEO_STATUS_PENDING,
    PendingFeedVideo,
)


class InMemoryPendingFeedVideoRepository:
    def __init__(self) -> None:
        self._rows: dict[str, PendingFeedVideo] = {}
        self._lock = threading.RLock()

    async def add(self, record: PendingFeedVideo) -> None:
        with self._lock:
            self._rows[record.id] = record

    async def get(self, record_id: str) -> PendingFeedVideo | None:
        with self._lock:
            return self._rows.get(record_id)

    async def list_due(
        self, *, now: datetime, limit: int = 50,
    ) -> list[PendingFeedVideo]:
        cutoff = ensure_utc(now)
        with self._lock:
            due = [
                row for row in self._rows.values()
                if row.status == PENDING_VIDEO_STATUS_PENDING
                and ensure_utc(row.next_poll_at) <= cutoff
            ]
        due.sort(key=lambda row: ensure_utc(row.next_poll_at))
        return due[:max(1, int(limit))]

    async def has_pending_for_character(self, character_id: str) -> bool:
        with self._lock:
            return any(
                row.character_id == character_id
                and row.status == PENDING_VIDEO_STATUS_PENDING
                for row in self._rows.values()
            )

    async def reschedule(
        self, record: PendingFeedVideo, *, next_poll_at: datetime, now: datetime,
    ) -> bool:
        with self._lock:
            current = self._rows.get(record.id)
            if current is None or current.status != PENDING_VIDEO_STATUS_PENDING:
                return False
            self._rows[record.id] = current.with_next_poll(
                next_poll_at=ensure_utc(next_poll_at), now=ensure_utc(now),
            )
            return True

    async def finish_if_pending(
        self,
        record_id: str,
        *,
        status: str,
        now: datetime,
        post_id: str | None = None,
        note: str = "",
    ) -> bool:
        """The SQL twin's compare-and-swap. The lock stands in for the
        database's row-level atomicity so two concurrent polls of the same
        finished job cannot both believe they may publish."""
        with self._lock:
            current = self._rows.get(record_id)
            if current is None or current.status != PENDING_VIDEO_STATUS_PENDING:
                return False
            self._rows[record_id] = current.finished(
                status, now=ensure_utc(now), post_id=post_id, note=note,
            )
            return True

    async def attach_post(
        self, record_id: str, *, post_id: str, now: datetime,
    ) -> bool:
        with self._lock:
            current = self._rows.get(record_id)
            if current is None:
                return False
            self._rows[record_id] = replace(
                current, post_id=post_id, updated_at=ensure_utc(now),
            )
            return True

    async def delete_finished_before(self, cutoff: datetime) -> int:
        cutoff_utc = ensure_utc(cutoff)
        with self._lock:
            stale = [
                row_id for row_id, row in self._rows.items()
                if row.is_terminal and ensure_utc(row.updated_at) < cutoff_utc
            ]
            for row_id in stale:
                del self._rows[row_id]
        return len(stale)
