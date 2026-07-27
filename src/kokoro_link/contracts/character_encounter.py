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

    async def claim_for_run(
        self, encounter_id: str, *, now: datetime,
    ) -> bool:
        """Atomically claim a ``planned`` encounter for running (P3-Dedup §3.4).

        CAS ``UPDATE ... SET status='running', started_at, updated_at WHERE
        id=? AND status='planned'``; returns ``True`` only for the caller that
        won the transition. Under a distributed race exactly one runner claims
        the encounter and produces the visible transcript/reflection; the loser
        gets ``False`` and must not run. ``list_runnable`` only surfaces
        ``planned`` rows, so single-runner behaviour is unchanged (the claim
        always succeeds for a due encounter)."""
