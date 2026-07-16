"""RSS source categorisation.

Open-ended string VO mirroring ``FeedKind`` / ``MemoryKind``. Lets the
operator declare what kind of feed a source is so per-character
``subscribed_categories`` can pre-filter the candidate pool before the
embedding-based curator does fine-grained ranking.

Categories are intentionally coarse. Fine matching is
embedding's job — categories only exist to keep an anime-only character
from even seeing finance feeds in their inbox window.
"""

from dataclasses import dataclass
from typing import ClassVar


@dataclass(frozen=True, slots=True)
class RssCategory:
    value: str

    NEWS: "ClassVar[RssCategory]"
    TECH: "ClassVar[RssCategory]"
    GAMING: "ClassVar[RssCategory]"
    ENTERTAINMENT: "ClassVar[RssCategory]"
    LIFESTYLE: "ClassVar[RssCategory]"
    SCIENCE: "ClassVar[RssCategory]"
    SPORTS: "ClassVar[RssCategory]"
    CULTURE: "ClassVar[RssCategory]"
    FINANCE: "ClassVar[RssCategory]"
    ANIME: "ClassVar[RssCategory]"
    HEALTH: "ClassVar[RssCategory]"
    WEATHER: "ClassVar[RssCategory]"
    EMERGENCY: "ClassVar[RssCategory]"
    TRAVEL: "ClassVar[RssCategory]"
    FOOD: "ClassVar[RssCategory]"
    EDUCATION: "ClassVar[RssCategory]"

    def __post_init__(self) -> None:
        if not self.value or not self.value.strip():
            raise ValueError("RssCategory value must be non-empty")
        object.__setattr__(self, "value", self.value.strip().lower())

    def __str__(self) -> str:
        return self.value

    @classmethod
    def from_string(cls, raw: str) -> "RssCategory":
        return cls(raw)


RssCategory.NEWS = RssCategory("news")
RssCategory.TECH = RssCategory("tech")
RssCategory.GAMING = RssCategory("gaming")
RssCategory.ENTERTAINMENT = RssCategory("entertainment")
RssCategory.LIFESTYLE = RssCategory("lifestyle")
RssCategory.SCIENCE = RssCategory("science")
RssCategory.SPORTS = RssCategory("sports")
RssCategory.CULTURE = RssCategory("culture")
RssCategory.FINANCE = RssCategory("finance")
RssCategory.ANIME = RssCategory("anime")
RssCategory.HEALTH = RssCategory("health")
RssCategory.WEATHER = RssCategory("weather")
RssCategory.EMERGENCY = RssCategory("emergency")
RssCategory.TRAVEL = RssCategory("travel")
RssCategory.FOOD = RssCategory("food")
RssCategory.EDUCATION = RssCategory("education")


CANONICAL_RSS_CATEGORIES: tuple[RssCategory, ...] = (
    RssCategory.NEWS,
    RssCategory.TECH,
    RssCategory.GAMING,
    RssCategory.ENTERTAINMENT,
    RssCategory.LIFESTYLE,
    RssCategory.SCIENCE,
    RssCategory.SPORTS,
    RssCategory.CULTURE,
    RssCategory.FINANCE,
    RssCategory.ANIME,
    RssCategory.HEALTH,
    RssCategory.WEATHER,
    RssCategory.EMERGENCY,
    RssCategory.TRAVEL,
    RssCategory.FOOD,
    RssCategory.EDUCATION,
)
