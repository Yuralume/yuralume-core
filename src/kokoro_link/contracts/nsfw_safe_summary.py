"""Safe-summary port for restricted chat turns.

NSFW mode stores raw message text for community-model continuity, but
frontier providers must not receive that raw text later. This port
generates a short replacement that preserves emotional continuity while
removing explicit details.
"""

from __future__ import annotations

from typing import Protocol

from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.conversation import Message


class NsfwSafeSummaryPort(Protocol):
    async def summarize(
        self,
        *,
        character: Character,
        message: Message,
        model: ChatModelPort | None = None,
        model_id: str | None = None,
    ) -> str:
        """Return a frontier-safe replacement for one restricted message.

        Implementations return ``""`` when the message is empty, not
        restricted, or the underlying generation fails. Callers then
        keep the raw message fail-closed: community providers may still
        see raw history, while frontier prompt assembly drops it.
        """
