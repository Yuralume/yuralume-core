"""RSS feed source descriptor.

A ``RssSource`` is a (URL, category, locale) triple that the ingestion
service polls on a schedule. Sources are seeded at startup from
``data/rss_sources.yaml`` (developer-curated) and may be enabled /
disabled at runtime by the operator. Health fields (``last_success_at``
/ ``last_error``) are written by the ingest service after each poll so
the admin UI can surface dead feeds without round-tripping the network.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime


@dataclass(frozen=True, slots=True)
class RssSource:
    id: str
    name: str
    feed_url: str
    category: str
    locale: str = "zh-TW"
    enabled: bool = True
    last_success_at: datetime | None = None
    last_error: str | None = None
    last_attempt_at: datetime | None = None
    fetched_count_total: int = 0
    """Cumulative count of events successfully ingested from this source.
    Useful for health check ('this feed is configured but never produces
    rows — broken parser?')."""
    default_for_categories: tuple[str, ...] = field(default_factory=tuple)
    """Optional override list of categories this source should apply to
    (in addition to its primary ``category``). Empty = primary only."""

    def __post_init__(self) -> None:
        if not self.id or not self.id.strip():
            raise ValueError("RssSource.id must be non-empty")
        if not self.feed_url or not self.feed_url.strip():
            raise ValueError("RssSource.feed_url must be non-empty")
        if not self.category or not self.category.strip():
            raise ValueError("RssSource.category must be non-empty")
        object.__setattr__(self, "id", self.id.strip())
        object.__setattr__(self, "name", (self.name or self.id).strip())
        object.__setattr__(self, "feed_url", self.feed_url.strip())
        object.__setattr__(self, "category", self.category.strip().lower())
        object.__setattr__(self, "locale", (self.locale or "zh-TW").strip())

    def with_success(
        self, *, at: datetime, fetched_count: int,
    ) -> "RssSource":
        return replace(
            self,
            last_attempt_at=at,
            last_success_at=at,
            last_error=None,
            fetched_count_total=self.fetched_count_total + max(0, fetched_count),
        )

    def with_error(self, *, at: datetime, error: str) -> "RssSource":
        return replace(
            self,
            last_attempt_at=at,
            last_error=(error or "unknown error")[:500],
        )
