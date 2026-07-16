"""Port for consolidating character-to-character social knowledge."""

from __future__ import annotations

from typing import Protocol

from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.character_peer_profile import CharacterPeerProfile
from kokoro_link.domain.entities.character_relationship import CharacterRelationship
from kokoro_link.domain.entities.memory_item import MemoryItem


class PeerKnowledgeConsolidatorPort(Protocol):
    async def consolidate(
        self,
        *,
        observer: Character,
        peer: Character,
        existing_profile: CharacterPeerProfile | None,
        relationship: CharacterRelationship,
        memories: list[MemoryItem],
    ) -> CharacterPeerProfile | None:
        """Return an updated directional peer profile.

        Implementations must be fail-soft: provider/parser failures
        should return ``None`` rather than raising into the scheduler.
        """
