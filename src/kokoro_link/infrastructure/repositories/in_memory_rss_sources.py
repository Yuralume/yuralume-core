"""In-memory RSS source repository for tests and no-database mode."""

from __future__ import annotations

from datetime import datetime
from dataclasses import replace

from kokoro_link.domain.entities.rss_source import RssSource


class InMemoryRssSourceRepository:
    def __init__(self) -> None:
        self._store: dict[str, RssSource] = {}

    async def list_all(self, *, enabled_only: bool = False) -> list[RssSource]:
        items = sorted(self._store.values(), key=lambda s: s.id)
        if enabled_only:
            return [s for s in items if s.enabled]
        return items

    async def get(self, source_id: str) -> RssSource | None:
        return self._store.get(source_id)

    async def upsert(self, source: RssSource) -> None:
        self._store[source.id] = source

    async def delete(self, source_id: str) -> None:
        self._store.pop(source_id, None)

    async def mark_success(
        self, source_id: str, *, at: datetime, fetched_count: int,
    ) -> None:
        existing = self._store.get(source_id)
        if existing is None:
            return
        self._store[source_id] = existing.with_success(
            at=at, fetched_count=fetched_count,
        )

    async def mark_error(
        self, source_id: str, *, at: datetime, error: str,
    ) -> None:
        existing = self._store.get(source_id)
        if existing is None:
            return
        self._store[source_id] = existing.with_error(at=at, error=error)
