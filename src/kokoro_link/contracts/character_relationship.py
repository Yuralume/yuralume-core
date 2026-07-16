"""Repository port for real character relationships."""

from __future__ import annotations

from typing import Protocol

from kokoro_link.domain.entities.character_relationship import CharacterRelationship


class CharacterRelationshipRepositoryPort(Protocol):
    async def get(self, relationship_id: str) -> CharacterRelationship | None:
        """Fetch one relationship by id."""

    async def get_pair(
        self, character_a_id: str, character_b_id: str,
    ) -> CharacterRelationship | None:
        """Fetch the canonical pair, regardless of caller order."""

    async def list_for_character(self, character_id: str) -> list[CharacterRelationship]:
        """Return relationships where the character is either side."""

    async def list_enabled(self) -> list[CharacterRelationship]:
        """Return every enabled relationship pair."""

    async def save(self, relationship: CharacterRelationship) -> None:
        """Upsert the relationship."""

    async def delete_for_character(self, character_id: str) -> int:
        """Delete every relationship containing the character."""
