"""Adapter contract for fetching one RSS feed URL.

Kept as a thin wrapper so the production adapter (feedparser) can be
swapped for a fake in tests without monkey-patching network calls.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True, slots=True)
class RawWorldEvent:
    """Pre-persistence shape produced by an RSS adapter.

    The persistence layer assigns ``id`` and ``fetched_at``; the adapter
    is only responsible for parsing what the feed publishes.
    """

    source_id: str
    source_name: str
    title: str
    summary: str
    url: str
    published_at: datetime
    category: str
    locale: str | None = None
    topic_tags: tuple[str, ...] = field(default_factory=tuple)


class RssFeedFetcherPort(Protocol):
    async def fetch(
        self,
        *,
        source_id: str,
        source_name: str,
        feed_url: str,
        category: str,
        locale: str | None = None,
    ) -> list[RawWorldEvent]:
        """Pull and parse one feed.

        Implementations must raise on transport failure (so the ingest
        service can mark the source unhealthy); they should *not* raise
        on individual entry parse failures — drop and continue.
        """
