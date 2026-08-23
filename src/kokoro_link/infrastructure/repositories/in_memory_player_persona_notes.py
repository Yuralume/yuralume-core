"""In-memory :class:`PlayerPersonaNote` store for tests / dev."""

from __future__ import annotations

from kokoro_link.contracts.player_persona_note import (
    PlayerPersonaNoteRepositoryPort,
)
from kokoro_link.domain.entities.player_persona_note import PlayerPersonaNote


class InMemoryPlayerPersonaNoteRepository(PlayerPersonaNoteRepositoryPort):
    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], PlayerPersonaNote] = {}

    async def get(
        self, *, character_id: str, operator_id: str,
    ) -> PlayerPersonaNote | None:
        return self._rows.get((character_id, operator_id))

    async def upsert(self, note: PlayerPersonaNote) -> None:
        self._rows[(note.character_id, note.operator_id)] = note

    async def delete(self, *, character_id: str, operator_id: str) -> bool:
        return self._rows.pop((character_id, operator_id), None) is not None
