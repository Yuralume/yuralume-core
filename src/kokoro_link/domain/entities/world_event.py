"""External-world event pulled from RSS / social feeds.

Kept separate from ``MemoryItem`` on purpose: world events are reference
material for prompt injection, not the character's subjective memory.
They are pooled globally (not per-character) and filtered at query time
by topic affinity / embedding similarity / category.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


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
    topic_tags: tuple[str, ...] = field(default_factory=tuple)
    embedding: list[float] | None = None
