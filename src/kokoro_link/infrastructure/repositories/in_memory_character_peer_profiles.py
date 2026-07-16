"""In-memory character peer profile repository."""

from __future__ import annotations

from kokoro_link.contracts.character_peer_profile import (
    CharacterPeerProfileRepositoryPort,
)
from kokoro_link.domain.entities.character_peer_profile import CharacterPeerProfile


def _pair_key(character_id: str, peer_character_id: str) -> tuple[str, str]:
    return character_id.strip(), peer_character_id.strip()


class InMemoryCharacterPeerProfileRepository(CharacterPeerProfileRepositoryPort):
    def __init__(self) -> None:
        self._items: dict[tuple[str, str], CharacterPeerProfile] = {}

    async def get(
        self,
        character_id: str,
        peer_character_id: str,
    ) -> CharacterPeerProfile | None:
        return self._items.get(_pair_key(character_id, peer_character_id))

    async def list_for_character(
        self,
        character_id: str,
    ) -> list[CharacterPeerProfile]:
        rows = [
            item for (owner_id, _), item in self._items.items()
            if owner_id == character_id
        ]
        rows.sort(key=lambda item: item.updated_at, reverse=True)
        return rows

    async def save(self, profile: CharacterPeerProfile) -> None:
        self._items[_pair_key(profile.character_id, profile.peer_character_id)] = profile

    async def delete_for_character(self, character_id: str) -> int:
        keys = [
            key for key, item in self._items.items()
            if item.character_id == character_id or item.peer_character_id == character_id
        ]
        for key in keys:
            del self._items[key]
        return len(keys)
