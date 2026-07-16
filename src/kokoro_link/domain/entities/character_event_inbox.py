"""Per-character event inbox entry.

An ``InboxItem`` is the result of the curator matching a global
``WorldEvent`` against the character's interest vector. It is *not* a
memory — feeding a row into a prompt does not make the character
"remember" the event; only user-driven engagement (chat, feed reaction)
goes through the existing memory path.

Surface-claim semantics: a row carries ``claimed_by_surface=None`` until
exactly one surface (proactive / feed / drama) calls
``CharacterEventInboxRepositoryPort.claim``, which atomically writes
both ``claimed_by_surface`` and ``claimed_at``. Subsequent claim
attempts return ``None``. This is what guarantees one news item is not
re-used across surfaces in the same window.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4


@dataclass(frozen=True, slots=True)
class CharacterEventInboxItem:
    id: str
    character_id: str
    world_event_id: str
    similarity: float
    created_at: datetime
    claimed_by_surface: str | None = None
    claimed_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.character_id or not self.character_id.strip():
            raise ValueError("character_id must be non-empty")
        if not self.world_event_id or not self.world_event_id.strip():
            raise ValueError("world_event_id must be non-empty")
        # Cosine similarity range; tolerate slight FP drift.
        if self.similarity < -1.01 or self.similarity > 1.01:
            raise ValueError("similarity must be in [-1, 1]")

    @classmethod
    def create(
        cls,
        *,
        character_id: str,
        world_event_id: str,
        similarity: float,
        created_at: datetime,
    ) -> "CharacterEventInboxItem":
        return cls(
            id=str(uuid4()),
            character_id=character_id,
            world_event_id=world_event_id,
            similarity=float(similarity),
            created_at=created_at,
        )

    @property
    def is_claimed(self) -> bool:
        return self.claimed_by_surface is not None
