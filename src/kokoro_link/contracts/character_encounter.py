"""Repository port for character encounters."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from kokoro_link.domain.entities.character_encounter import CharacterEncounter


class CharacterEncounterRepositoryPort(Protocol):
    async def get(self, encounter_id: str) -> CharacterEncounter | None:
        """Fetch one encounter by id."""

    async def save(self, encounter: CharacterEncounter) -> None:
        """Upsert an encounter."""

    async def list_for_character(
        self, character_id: str, *, limit: int = 30,
    ) -> list[CharacterEncounter]:
        """Return recent encounters involving the character."""

    async def list_for_relationship(
        self, relationship_id: str, *, limit: int = 30,
    ) -> list[CharacterEncounter]:
        """Return recent encounters for a pair."""

    async def list_runnable(self, now: datetime, *, limit: int = 20) -> list[CharacterEncounter]:
        """Return planned encounters due at or before ``now``."""

    async def count_for_character_since(self, character_id: str, since: datetime) -> int:
        """Count completed/planned/running encounters for a character since ``since``."""

    async def has_pending_for_relationship(self, relationship_id: str) -> bool:
        """Return true when the pair already has a planned/running encounter."""

    async def delete_for_character(self, character_id: str) -> int:
        """Delete encounters involving the character."""
