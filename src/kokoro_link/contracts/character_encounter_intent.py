"""Repository port for chat-extracted character encounter intents."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from kokoro_link.domain.entities.character_encounter_intent import (
    CharacterEncounterIntent,
)


class CharacterEncounterIntentRepositoryPort(Protocol):
    async def get(self, intent_id: str) -> CharacterEncounterIntent | None:
        """Fetch one intent by id."""

    async def save(self, intent: CharacterEncounterIntent) -> None:
        """Upsert an intent."""

    async def add(self, intent: CharacterEncounterIntent) -> None:
        """Insert an intent."""

    async def find_pending_for_pair(
        self,
        character_a_id: str,
        character_b_id: str,
        *,
        now: datetime,
        horizon: datetime,
    ) -> CharacterEncounterIntent | None:
        """Return the oldest pending intent for this unordered pair."""

    async def list_pending_for_character(
        self, character_id: str, *, now: datetime, limit: int = 30,
    ) -> list[CharacterEncounterIntent]:
        """Return pending intents involving the character."""

    async def delete_for_character(self, character_id: str) -> int:
        """Delete intents involving the character."""
