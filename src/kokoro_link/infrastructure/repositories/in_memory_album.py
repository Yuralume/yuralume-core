"""In-process ``AlbumRepositoryPort`` — list-backed."""

from __future__ import annotations

from collections import defaultdict

from kokoro_link.contracts.album import AlbumRepositoryPort
from kokoro_link.domain.entities.album_item import AlbumItem


class InMemoryAlbumRepository(AlbumRepositoryPort):
    def __init__(self) -> None:
        self._by_character: dict[str, list[AlbumItem]] = defaultdict(list)
        self._by_id: dict[str, AlbumItem] = {}

    async def add(self, item: AlbumItem) -> None:
        if item.id in self._by_id:
            raise ValueError(f"album item {item.id!r} already exists")
        self._by_character[item.character_id].append(item)
        self._by_id[item.id] = item

    async def get(self, item_id: str) -> AlbumItem | None:
        return self._by_id.get(item_id)

    async def list_for_character(
        self,
        character_id: str,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[AlbumItem]:
        items = list(self._by_character.get(character_id, []))
        # Newest first — caller expects this ordering for the grid UI
        items.sort(key=lambda i: i.created_at, reverse=True)
        if offset:
            items = items[offset:]
        if limit is not None:
            items = items[:limit]
        return items

    async def count_for_character(self, character_id: str) -> int:
        return len(self._by_character.get(character_id, []))

    async def delete(self, item_id: str) -> bool:
        existing = self._by_id.pop(item_id, None)
        if existing is None:
            return False
        bucket = self._by_character.get(existing.character_id, [])
        self._by_character[existing.character_id] = [
            it for it in bucket if it.id != item_id
        ]
        return True

    async def delete_for_character(self, character_id: str) -> int:
        removed = self._by_character.pop(character_id, [])
        for item in removed:
            self._by_id.pop(item.id, None)
        return len(removed)
