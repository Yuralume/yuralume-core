"""Repository port for :class:`PlayerPersonaNote`."""

from __future__ import annotations

from typing import Protocol

from kokoro_link.domain.entities.player_persona_note import PlayerPersonaNote


class PlayerPersonaNoteRepositoryPort(Protocol):
    async def get(
        self, *, character_id: str, operator_id: str,
    ) -> PlayerPersonaNote | None:
        """Return the declared note for the pair, or ``None`` when unset.

        Per-pair isolation: a note declared to one character must not be
        visible to a sibling, and never to another operator."""

    async def upsert(self, note: PlayerPersonaNote) -> None:
        """Persist (insert or update) the note for the pair."""

    async def delete(self, *, character_id: str, operator_id: str) -> bool:
        """Remove the pair's note. Returns whether a row was deleted.

        Clearing is deletion rather than an empty string so the prompt
        path reads "no row = nothing declared" without having to know
        that an empty note means the same thing."""
