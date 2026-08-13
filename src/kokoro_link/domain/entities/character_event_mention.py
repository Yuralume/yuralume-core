"""Record that a character actually raised a world event with its player.

The event inbox answers "what could this character bring up"; a mention
answers "what did it already bring up". They are different facts and the
first cannot stand in for the second: an inbox row is *claimed* the
moment a surface consumes it, and a claimed row never appears in the
chat peek again. So the instant a proactive DM says "有人在板上吵那家
拉麵店", the material behind it leaves the chat prompt — and the
follow-up question the player asks two minutes later is unanswerable.

A mention is deliberately **not** a ``MemoryItem``. It is a pointer to
reference material the character used, not a subjective recollection;
memory still forms the normal way, through the conversation the player
and character actually have about it.

Surface is recorded (not just the pair) so it stays possible to tell
"I sent you this in a DM" from "I posted about it" once other surfaces
start writing mentions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4


@dataclass(frozen=True, slots=True)
class CharacterEventMention:
    id: str
    character_id: str
    world_event_id: str
    surface: str
    mentioned_at: datetime

    def __post_init__(self) -> None:
        if not self.character_id or not self.character_id.strip():
            raise ValueError("character_id must be non-empty")
        if not self.world_event_id or not self.world_event_id.strip():
            raise ValueError("world_event_id must be non-empty")
        if not self.surface or not self.surface.strip():
            raise ValueError("surface must be non-empty")

    @classmethod
    def create(
        cls,
        *,
        character_id: str,
        world_event_id: str,
        surface: str,
        mentioned_at: datetime,
    ) -> "CharacterEventMention":
        return cls(
            id=str(uuid4()),
            character_id=character_id,
            world_event_id=world_event_id,
            surface=surface,
            mentioned_at=mentioned_at,
        )
