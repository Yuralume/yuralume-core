"""Repository contract for "this character already raised this event".

Writes are idempotent per ``(character_id, world_event_id)``: raising the
same event twice is one relationship with a fresher timestamp, not two
rows. Reads are newest-first because the consumer (the chat prompt) wants
the tail, and a chat turn must never wait on this — a mention is context,
not correctness.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from kokoro_link.domain.entities.character_event_mention import (
    CharacterEventMention,
)


class CharacterEventMentionRepositoryPort(Protocol):
    async def record(self, mention: CharacterEventMention) -> None:
        """Upsert one mention.

        A repeat mention of the same event refreshes ``mentioned_at``
        and ``surface`` rather than inserting a second row, so the
        recall block shows when the character *last* brought it up."""
        ...

    async def list_recent(
        self, character_id: str, *, limit: int = 5,
    ) -> list[CharacterEventMention]:
        """Most recently mentioned first, newest ``mentioned_at`` first."""
        ...

    async def delete_for_character(self, character_id: str) -> int:
        """Purge every mention of one character. Returns rows deleted."""
        ...

    async def delete_older_than(self, cutoff: datetime) -> int:
        """Drop mentions older than ``cutoff``. Returns rows deleted.

        The table also has ``ON DELETE CASCADE`` from ``world_events``,
        but foreign keys are not enforced on self-host SQLite (the
        engine never sets ``PRAGMA foreign_keys=ON``), so this sweep is
        what actually bounds the table there."""
        ...
