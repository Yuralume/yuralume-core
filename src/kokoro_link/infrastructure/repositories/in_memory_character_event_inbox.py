"""In-memory character event inbox repository.

Mirrors the SQLAlchemy implementation's claim semantics: claim is
atomic with respect to a single coroutine event loop (no thread races
to worry about), so a simple ``if claimed_by_surface is None`` check
suffices.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from kokoro_link.domain.entities.character_event_inbox import (
    CharacterEventInboxItem,
)


class InMemoryCharacterEventInboxRepository:
    def __init__(self) -> None:
        self._store: dict[str, CharacterEventInboxItem] = {}

    async def add_many(
        self, items: list[CharacterEventInboxItem],
    ) -> None:
        for item in items:
            # Mirror unique (character_id, world_event_id) — skip rather
            # than overwrite so the curator can rerun without erasing
            # claim history on existing rows.
            duplicate = any(
                e.character_id == item.character_id
                and e.world_event_id == item.world_event_id
                for e in self._store.values()
            )
            if duplicate:
                continue
            self._store[item.id] = item

    async def list_for_character(
        self,
        character_id: str,
        *,
        unclaimed_only: bool = False,
        surface: str | None = None,
        limit: int | None = None,
    ) -> list[CharacterEventInboxItem]:
        items = [
            i for i in self._store.values()
            if i.character_id == character_id
        ]
        if unclaimed_only:
            items = [i for i in items if i.claimed_by_surface is None]
        elif surface is not None:
            items = [i for i in items if i.claimed_by_surface == surface]
        items.sort(key=lambda i: i.created_at)
        if limit is not None:
            return items[:limit]
        return items

    async def claim(
        self, item_id: str, *, surface: str, at: datetime,
    ) -> CharacterEventInboxItem | None:
        existing = self._store.get(item_id)
        if existing is None or existing.claimed_by_surface is not None:
            return None
        updated = replace(
            existing, claimed_by_surface=surface, claimed_at=at,
        )
        self._store[item_id] = updated
        return updated

    async def release(
        self, item_id: str, *, surface: str,
    ) -> bool:
        existing = self._store.get(item_id)
        if existing is None or existing.claimed_by_surface != surface:
            return False
        self._store[item_id] = replace(
            existing, claimed_by_surface=None, claimed_at=None,
        )
        return True

    async def count_unclaimed(self, character_id: str) -> int:
        return sum(
            1 for i in self._store.values()
            if i.character_id == character_id and i.claimed_by_surface is None
        )

    async def trim_oldest(
        self, character_id: str, *, keep: int,
    ) -> int:
        items = [
            i for i in self._store.values()
            if i.character_id == character_id
        ]
        items.sort(key=lambda i: i.created_at, reverse=True)
        to_delete = items[max(0, keep):]
        for item in to_delete:
            self._store.pop(item.id, None)
        return len(to_delete)

    async def delete_older_than(self, cutoff: datetime) -> int:
        to_delete = [
            iid for iid, i in self._store.items() if i.created_at < cutoff
        ]
        for iid in to_delete:
            self._store.pop(iid, None)
        return len(to_delete)

    async def delete_for_event(self, world_event_id: str) -> int:
        to_delete = [
            iid for iid, i in self._store.items()
            if i.world_event_id == world_event_id
        ]
        for iid in to_delete:
            self._store.pop(iid, None)
        return len(to_delete)

    async def has_event(
        self, character_id: str, world_event_id: str,
    ) -> bool:
        return any(
            i.character_id == character_id
            and i.world_event_id == world_event_id
            for i in self._store.values()
        )
