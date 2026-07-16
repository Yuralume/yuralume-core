"""Repository port for directional character peer profiles."""

from __future__ import annotations

from typing import Protocol

from kokoro_link.domain.entities.character_peer_profile import CharacterPeerProfile


class CharacterPeerProfileRepositoryPort(Protocol):
    async def get(
        self,
        character_id: str,
        peer_character_id: str,
    ) -> CharacterPeerProfile | None:
        """Fetch the directional profile for ``character_id -> peer``."""

    async def list_for_character(
        self,
        character_id: str,
    ) -> list[CharacterPeerProfile]:
        """Return every peer profile owned by one observer character."""

    async def save(self, profile: CharacterPeerProfile) -> None:
        """Upsert one profile by its directional character pair."""

    async def delete_for_character(self, character_id: str) -> int:
        """Delete profiles where the character is observer or peer."""
