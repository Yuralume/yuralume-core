"""External-world event pulled from RSS / social feeds.

Kept separate from ``MemoryItem`` on purpose: world events are reference
material for prompt injection, not the character's subjective memory.
They are pooled globally (not per-character) and filtered at query time
by topic affinity / embedding similarity / category.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from kokoro_link.domain.value_objects.region_code import normalise_region


@dataclass(frozen=True, slots=True)
class WorldEvent:
    id: str
    source: str
    title: str
    summary: str
    url: str
    published_at: datetime
    fetched_at: datetime
    category: str = "news"
    """Coarse RSS category (matches ``RssCategory`` values). ``"news"``
    is the safe default for legacy rows persisted before the column
    existed; the curator treats that as 'matches anyone with an
    interest in news / general'. Free-form string so new categories
    don't require a migration."""
    locale: str | None = None
    """Locale / source region carried from the RSS source registry.

    This is a fact for downstream LLM prompts, not a service-side
    filter. Legacy rows may be ``None``.
    """
    source_region: str | None = None
    """Geographic binding of the originating ``RssSource`` (``TW`` / ``JP``…).

    Denormalised from ``rss_sources.region`` at ingest time: the pool is
    queried on every curator pass and the value is immutable for a given
    event, so a join would cost the hot path nothing but latency.

    ``None`` means *global* — curated for every player. The curator keeps
    ``NULL`` ∪ the operator's own region and drops the rest, so a Japanese
    player's character never reads Taiwanese local news. Distinct from
    ``locale``, which is the publishing *language* and stays a
    prompt-visible fact only.
    """
    topic_tags: tuple[str, ...] = field(default_factory=tuple)
    embedding: list[float] | None = None

    def __post_init__(self) -> None:
        # Region comparisons happen in SQL and in Python; normalising at
        # construction keeps both sides on the same casing without every
        # call site remembering to.
        object.__setattr__(
            self, "source_region", normalise_region(self.source_region),
        )
