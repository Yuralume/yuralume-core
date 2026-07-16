"""Application service for operator-approved character relationships."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from kokoro_link.contracts.character_relationship import (
    CharacterRelationshipRepositoryPort,
)
from kokoro_link.contracts.repositories import CharacterRepositoryPort
from kokoro_link.domain.entities.character_relationship import (
    CharacterRelationship,
    clamp_score,
)


class CharacterRelationshipError(ValueError):
    pass


class CharacterRelationshipNotFoundError(CharacterRelationshipError):
    pass


class CharacterRelationshipValidationError(CharacterRelationshipError):
    pass


@dataclass(frozen=True, slots=True)
class CharacterRelationshipUpdate:
    enabled: bool | None = None
    relationship_label: str | None = None
    how_a_sees_b: str | None = None
    how_b_sees_a: str | None = None
    affection_a_to_b: int | None = None
    affection_b_to_a: int | None = None
    trust_a_to_b: int | None = None
    trust_b_to_a: int | None = None
    last_interaction_at: datetime | None = None


def canonical_pair(a: str, b: str) -> tuple[str, str]:
    first = a.strip()
    second = b.strip()
    if not first or not second:
        raise CharacterRelationshipValidationError("Character ids must be non-empty")
    if first == second:
        raise CharacterRelationshipValidationError("Cannot relate a character to itself")
    return tuple(sorted((first, second)))  # type: ignore[return-value]


class CharacterRelationshipService:
    def __init__(
        self,
        *,
        repository: CharacterRelationshipRepositoryPort,
        character_repository: CharacterRepositoryPort,
    ) -> None:
        self._repository = repository
        self._characters = character_repository

    async def list_for_character(self, character_id: str) -> list[CharacterRelationship]:
        if await self._characters.get(character_id) is None:
            raise CharacterRelationshipNotFoundError("Character not found")
        return await self._repository.list_for_character(character_id)

    async def create_or_enable(
        self,
        *,
        character_id: str,
        target_character_id: str,
        relationship_label: str = "",
        how_a_sees_b: str = "",
        how_b_sees_a: str = "",
    ) -> CharacterRelationship:
        a, b = canonical_pair(character_id, target_character_id)
        if await self._characters.get(a) is None or await self._characters.get(b) is None:
            raise CharacterRelationshipNotFoundError("Character not found")
        existing = await self._repository.get_pair(a, b)
        if existing is not None:
            updated = existing.with_updates(
                enabled=True,
                relationship_label=relationship_label or existing.relationship_label,
                how_a_sees_b=how_a_sees_b or existing.how_a_sees_b,
                how_b_sees_a=how_b_sees_a or existing.how_b_sees_a,
            )
            await self._repository.save(updated)
            return updated
        relationship = CharacterRelationship.create(
            character_a_id=a,
            character_b_id=b,
            enabled=True,
            relationship_label=relationship_label,
            how_a_sees_b=how_a_sees_b,
            how_b_sees_a=how_b_sees_a,
        )
        await self._repository.save(relationship)
        return relationship

    async def update(
        self,
        relationship_id: str,
        update: CharacterRelationshipUpdate,
    ) -> CharacterRelationship:
        existing = await self._repository.get(relationship_id)
        if existing is None:
            raise CharacterRelationshipNotFoundError("Relationship not found")
        updated = existing.with_updates(
            enabled=update.enabled,
            relationship_label=update.relationship_label,
            how_a_sees_b=update.how_a_sees_b,
            how_b_sees_a=update.how_b_sees_a,
            affection_a_to_b=(
                None if update.affection_a_to_b is None
                else clamp_score(update.affection_a_to_b)
            ),
            affection_b_to_a=(
                None if update.affection_b_to_a is None
                else clamp_score(update.affection_b_to_a)
            ),
            trust_a_to_b=(
                None if update.trust_a_to_b is None
                else clamp_score(update.trust_a_to_b)
            ),
            trust_b_to_a=(
                None if update.trust_b_to_a is None
                else clamp_score(update.trust_b_to_a)
            ),
            last_interaction_at=update.last_interaction_at,
        )
        await self._repository.save(updated)
        return updated

    async def apply_reflection(
        self,
        relationship_id: str,
        *,
        affection_a_delta: int = 0,
        affection_b_delta: int = 0,
        trust_a_delta: int = 0,
        trust_b_delta: int = 0,
        how_a_sees_b: str | None = None,
        how_b_sees_a: str | None = None,
        interacted_at: datetime,
    ) -> CharacterRelationship:
        relationship = await self._repository.get(relationship_id)
        if relationship is None:
            raise CharacterRelationshipNotFoundError("Relationship not found")
        updated = relationship.with_updates(
            affection_a_to_b=clamp_score(
                relationship.affection_a_to_b + affection_a_delta,
            ),
            affection_b_to_a=clamp_score(
                relationship.affection_b_to_a + affection_b_delta,
            ),
            trust_a_to_b=clamp_score(relationship.trust_a_to_b + trust_a_delta),
            trust_b_to_a=clamp_score(relationship.trust_b_to_a + trust_b_delta),
            how_a_sees_b=how_a_sees_b or relationship.how_a_sees_b,
            how_b_sees_a=how_b_sees_a or relationship.how_b_sees_a,
            last_interaction_at=interacted_at,
        )
        await self._repository.save(updated)
        return updated
