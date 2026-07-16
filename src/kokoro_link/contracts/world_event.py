"""Repository contract for world events."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from kokoro_link.domain.entities.world_event import WorldEvent


class WorldEventRepositoryPort(Protocol):
    async def query_recent(
        self,
        *,
        limit: int,
        topic_tags: list[str] | None = None,
        max_age_days: int | None = None,
    ) -> list[WorldEvent]: ...

    async def upsert(self, event: WorldEvent) -> None: ...

    async def delete_older_than(self, cutoff: datetime) -> int: ...

    async def get(self, event_id: str) -> WorldEvent | None: ...

    async def has_url(self, url: str) -> bool:
        """De-dup probe used by the ingest service to skip events whose
        URL was already persisted (RSS feeds republish entries on
        edits / re-orderings)."""

    async def list_with_embeddings_in_window(
        self,
        *,
        since: datetime,
        categories: list[str] | None = None,
        limit: int = 500,
    ) -> list[WorldEvent]:
        """List events with non-null embeddings published since
        ``since``. ``categories`` (when non-empty) filters to those
        categories only. The curator pulls a window then ranks in
        Python — pgvector can rank server-side, but the per-character
        compute is small (≤ 500 × 1024 floats) and Python ranking keeps
        the port DB-agnostic."""
