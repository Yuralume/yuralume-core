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

from kokoro_link.domain.value_objects.region_code import normalise_region


@dataclass(frozen=True, slots=True)
class RssSource:
    id: str
    name: str
    feed_url: str
    category: str
    locale: str = "zh-TW"
    region: str | None = None
    """Geographic binding of the source's *content* (``TW`` / ``JP`` …).

    ``None`` means the source is global — relevant to every player. A
    region-tagged source is only curated into characters whose owner
    lives in that region (see ``EventCuratorService``); an operator with
    no ``country_code`` (the self-host default) is never filtered, so
    self-host behaviour is unchanged. Normalised to upper case; blank
    collapses to ``None`` so the admin form can clear it.

    Orthogonal to ``locale`` (the language the feed publishes in): a
    Taiwanese outlet publishing international news in zh-TW is
    ``locale=zh-TW, region=TW`` — useless to a Japanese player even
    though the topic is global.
    """
    region_locked: bool = False
    """``True`` once an operator has explicitly set *or cleared* ``region``.

    ``region is None`` alone cannot distinguish "never tagged" (a row from
    before the column existed, which the YAML seed must still backfill)
    from "deliberately made global" (an admin clear, which the seed must
    never undo). This flag carries that intent: the seed backfills only
    while it is ``False``, and only the admin API ever sets it.
    """
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
        object.__setattr__(
            self, "region", normalise_region(self.region),
        )

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

    def with_error(
        self, *, at: datetime, error: str, fetched_count: int = 0,
    ) -> "RssSource":
        return replace(
            self,
            last_attempt_at=at,
            last_error=(error or "unknown error")[:500],
            fetched_count_total=(
                self.fetched_count_total + max(0, fetched_count)
            ),
        )
