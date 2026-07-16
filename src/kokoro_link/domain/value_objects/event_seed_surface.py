"""Surfaces that consume a curated world-event seed.

A world event lives in a per-character inbox until exactly one surface
``claim``s it; once claimed it is locked to that surface forever. This
keeps proactive messages, LumeGram posts, and branching-drama hooks
from all leaning on the same news item in the same week.
"""

from dataclasses import dataclass
from typing import ClassVar


@dataclass(frozen=True, slots=True)
class EventSeedSurface:
    value: str

    PROACTIVE_MESSAGE: "ClassVar[EventSeedSurface]"
    """Proactive scheduler — character DMs the user about the event."""
    FEED_POST: "ClassVar[EventSeedSurface]"
    """LumeGram autonomous post built around the event."""
    BRANCHING_DRAMA: "ClassVar[EventSeedSurface]"
    """Branching-drama scene seed inspired by the event."""

    def __post_init__(self) -> None:
        if not self.value or not self.value.strip():
            raise ValueError("EventSeedSurface value must be non-empty")
        object.__setattr__(self, "value", self.value.strip().lower())

    def __str__(self) -> str:
        return self.value

    @classmethod
    def from_string(cls, raw: str) -> "EventSeedSurface":
        return cls(raw)


EventSeedSurface.PROACTIVE_MESSAGE = EventSeedSurface("proactive_message")
EventSeedSurface.FEED_POST = EventSeedSurface("feed_post")
EventSeedSurface.BRANCHING_DRAMA = EventSeedSurface("branching_drama")


CANONICAL_EVENT_SEED_SURFACES: tuple[EventSeedSurface, ...] = (
    EventSeedSurface.PROACTIVE_MESSAGE,
    EventSeedSurface.FEED_POST,
    EventSeedSurface.BRANCHING_DRAMA,
)
