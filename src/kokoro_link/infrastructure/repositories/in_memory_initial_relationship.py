from __future__ import annotations

from kokoro_link.contracts.initial_relationship import (
    CharacterOperatorRelationshipSeedRepositoryPort,
)
from kokoro_link.domain.entities.character_operator_relationship_seed import (
    CharacterOperatorRelationshipSeed,
)


class InMemoryCharacterOperatorRelationshipSeedRepository(
    CharacterOperatorRelationshipSeedRepositoryPort,
):
    def __init__(self) -> None:
        self._seeds: dict[tuple[str, str], CharacterOperatorRelationshipSeed] = {}

    async def get(
        self, character_id: str, operator_id: str,
    ) -> CharacterOperatorRelationshipSeed | None:
        return self._seeds.get((character_id, operator_id))

    async def save(self, seed: CharacterOperatorRelationshipSeed) -> None:
        self._seeds[(seed.character_id, seed.operator_id)] = seed

    async def delete_for_character(self, character_id: str) -> int:
        keys = [key for key in self._seeds if key[0] == character_id]
        for key in keys:
            del self._seeds[key]
        return len(keys)
