"""In-memory ``CharacterEventMentionRepositoryPort`` (tests / no-DB mode).

Mirrors the SQL adapter's semantics, including the unique
``(character_id, world_event_id)`` pair: a repeat mention refreshes the
existing row rather than adding a second one.
"""

from __future__ import annotations

from datetime import datetime

from kokoro_link.domain.entities.character_event_mention import (
    CharacterEventMention,
)


class InMemoryCharacterEventMentionRepository:
    def __init__(self) -> None:
        self._store: dict[tuple[str, str], CharacterEventMention] = {}

    async def record(self, mention: CharacterEventMention) -> None:
        key = (mention.character_id, mention.world_event_id)
        existing = self._store.get(key)
        # Keep the original row id on refresh so callers holding an id
        # (and the SQL adapter, whose upsert never rewrites the primary
        # key) observe the same thing.
        self._store[key] = (
            mention if existing is None
            else CharacterEventMention(
                id=existing.id,
                character_id=mention.character_id,
                world_event_id=mention.world_event_id,
                surface=mention.surface,
                mentioned_at=mention.mentioned_at,
            )
        )

    async def list_recent(
        self, character_id: str, *, limit: int = 5,
    ) -> list[CharacterEventMention]:
        if limit <= 0:
            return []
        rows = [
            mention for mention in self._store.values()
            if mention.character_id == character_id
        ]
        rows.sort(key=lambda m: m.mentioned_at, reverse=True)
        return rows[:limit]

    async def delete_for_character(self, character_id: str) -> int:
        keys = [
            key for key, mention in self._store.items()
            if mention.character_id == character_id
        ]
        for key in keys:
            del self._store[key]
        return len(keys)

    async def delete_older_than(self, cutoff: datetime) -> int:
        keys = [
            key for key, mention in self._store.items()
            if mention.mentioned_at < cutoff
        ]
        for key in keys:
            del self._store[key]
        return len(keys)
