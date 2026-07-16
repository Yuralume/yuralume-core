"""Repository port for initial character/operator relationship seed."""

from __future__ import annotations

from typing import Protocol

from kokoro_link.domain.entities.character_operator_relationship_seed import (
    CharacterOperatorRelationshipSeed,
)


class CharacterOperatorRelationshipSeedRepositoryPort(Protocol):
    async def get(
        self, character_id: str, operator_id: str,
    ) -> CharacterOperatorRelationshipSeed | None:
        """Return the seed for one pair, if present."""

    async def save(self, seed: CharacterOperatorRelationshipSeed) -> None:
        """Insert or replace the seed for ``(character_id, operator_id)``."""

    async def delete_for_character(self, character_id: str) -> int:
        """Delete all seeds owned by a character."""
