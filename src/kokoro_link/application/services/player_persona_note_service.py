"""Read / write the player's declared persona note for one pair.

Thin on purpose: the whole feature is one string per
``(character_id, operator_id)``. The service exists so the route and the
prompt path share one definition of "clearing is deleting" and one
validation entry point — the length ceiling lives in the entity, and
every writer goes through here to hit it.
"""

from __future__ import annotations

from datetime import datetime, timezone

from kokoro_link.contracts.player_persona_note import (
    PlayerPersonaNoteRepositoryPort,
)
from kokoro_link.domain.entities.player_persona_note import PlayerPersonaNote


class PlayerPersonaNoteService:
    def __init__(self, repository: PlayerPersonaNoteRepositoryPort) -> None:
        self._repository = repository

    async def get(
        self, *, character_id: str, operator_id: str,
    ) -> PlayerPersonaNote | None:
        return await self._repository.get(
            character_id=character_id, operator_id=operator_id,
        )

    async def set(
        self,
        *,
        character_id: str,
        operator_id: str,
        note: str,
        now: datetime | None = None,
    ) -> PlayerPersonaNote | None:
        """Write the declaration, or clear it when ``note`` is blank.

        Returns the stored note, or ``None`` when the pair now has no
        declaration. Raises ``ValueError`` when the note exceeds the
        entity's ceiling — the API layer rejects over-length input before
        this, but a background / test caller must not be able to slip a
        1000-character premise into every prompt.
        """
        cleaned = (note or "").strip()
        if not cleaned:
            await self._repository.delete(
                character_id=character_id, operator_id=operator_id,
            )
            return None
        stored = PlayerPersonaNote(
            character_id=character_id,
            operator_id=operator_id,
            note=cleaned,
            updated_at=now or datetime.now(timezone.utc),
        )
        await self._repository.upsert(stored)
        return stored
