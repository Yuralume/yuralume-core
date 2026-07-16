"""Memory classification.

``MemoryKind`` is intentionally string-based so new kinds can be added
without breaking storage or serialization. The constants below are the
canonical set used by the MVP; callers should prefer these but must
tolerate unknown values when reading from persistence.
"""

from dataclasses import dataclass
from typing import ClassVar


@dataclass(frozen=True, slots=True)
class MemoryKind:
    """A classification label for a memory item.

    Using a value object (not an Enum) keeps the set open-ended: future
    phases can introduce new kinds without schema migrations or enum
    updates. Equality and hashing are driven by the underlying string.
    """

    value: str

    # Canonical kinds used by the current extractor and prompt renderer.
    EPISODIC: "ClassVar[MemoryKind]"
    SEMANTIC: "ClassVar[MemoryKind]"
    RELATIONSHIP: "ClassVar[MemoryKind]"
    REFLECTION: "ClassVar[MemoryKind]"
    HEARSAY: "ClassVar[MemoryKind]"
    RELATIONSHIP_MILESTONE: "ClassVar[MemoryKind]"

    def __post_init__(self) -> None:
        if not self.value or not self.value.strip():
            raise ValueError("MemoryKind value must be non-empty")
        # Normalize to lowercase so lookups are case-insensitive.
        object.__setattr__(self, "value", self.value.strip().lower())

    def __str__(self) -> str:
        return self.value

    @classmethod
    def from_string(cls, raw: str) -> "MemoryKind":
        return cls(raw)


MemoryKind.EPISODIC = MemoryKind("episodic")
MemoryKind.SEMANTIC = MemoryKind("semantic")
MemoryKind.RELATIONSHIP = MemoryKind("relationship")
MemoryKind.REFLECTION = MemoryKind("reflection")
MemoryKind.HEARSAY = MemoryKind("hearsay")
MemoryKind.RELATIONSHIP_MILESTONE = MemoryKind("relationship_milestone")
"""Anchor row written by the dream pass when the interaction-volume band
crosses a threshold (e.g. stranger -> acquaintance). Rendered as its own
prompt block so milestone moments don't drown in the regular episodic
stream, per HUMANIZATION_ROADMAP §3.5."""


CANONICAL_KINDS: tuple[MemoryKind, ...] = (
    MemoryKind.SEMANTIC,
    MemoryKind.RELATIONSHIP,
    MemoryKind.RELATIONSHIP_MILESTONE,
    MemoryKind.EPISODIC,
    MemoryKind.HEARSAY,
    MemoryKind.REFLECTION,
)
"""Preferred display order for grouped prompt rendering.

Semantic facts are most useful as grounding, followed by relationship
context, past events, reported second-hand information, and finally the
character's own reflections.
"""
