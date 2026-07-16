"""Repository contract for ``RssSource`` rows."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from kokoro_link.domain.entities.rss_source import RssSource


class RssSourceRepositoryPort(Protocol):
    async def list_all(self, *, enabled_only: bool = False) -> list[RssSource]: ...

    async def get(self, source_id: str) -> RssSource | None: ...

    async def upsert(self, source: RssSource) -> None: ...

    async def delete(self, source_id: str) -> None: ...

    async def mark_success(
        self, source_id: str, *, at: datetime, fetched_count: int,
    ) -> None: ...

    async def mark_error(
        self, source_id: str, *, at: datetime, error: str,
    ) -> None: ...
