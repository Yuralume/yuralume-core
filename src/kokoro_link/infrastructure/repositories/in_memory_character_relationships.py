"""In-memory character relationship repository."""

from __future__ import annotations

from kokoro_link.contracts.character_relationship import (
    CharacterRelationshipRepositoryPort,
)
from kokoro_link.domain.entities.character_relationship import CharacterRelationship


def canonical_pair(a: str, b: str) -> tuple[str, str]:
    first = a.strip()
    second = b.strip()
    if first == second:
        raise ValueError("Relationship cannot point to the same character")
    return tuple(sorted((first, second)))  # type: ignore[return-value]


class InMemoryCharacterRelationshipRepository(CharacterRelationshipRepositoryPort):
    def __init__(self) -> None:
        self._items: dict[str, CharacterRelationship] = {}

    async def get(self, relationship_id: str) -> CharacterRelationship | None:
        return self._items.get(relationship_id)

    async def get_pair(
        self, character_a_id: str, character_b_id: str,
    ) -> CharacterRelationship | None:
        a, b = canonical_pair(character_a_id, character_b_id)
        for item in self._items.values():
            if item.character_a_id == a and item.character_b_id == b:
                return item
        return None

    async def list_for_character(self, character_id: str) -> list[CharacterRelationship]:
        rows = [
            item for item in self._items.values()
            if item.character_a_id == character_id or item.character_b_id == character_id
        ]
        rows.sort(key=lambda item: item.updated_at, reverse=True)
        return rows

    async def list_enabled(self) -> list[CharacterRelationship]:
        rows = [item for item in self._items.values() if item.enabled]
        rows.sort(key=lambda item: item.updated_at, reverse=True)
        return rows

    async def save(self, relationship: CharacterRelationship) -> None:
        self._items[relationship.id] = relationship

    async def delete_for_character(self, character_id: str) -> int:
        target = [
            item_id for item_id, item in self._items.items()
            if item.character_a_id == character_id or item.character_b_id == character_id
        ]
        for item_id in target:
            del self._items[item_id]
        return len(target)
