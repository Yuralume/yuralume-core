"""Memory extractor port.

Given a completed chat turn, an extractor produces zero or more
``MemoryItem`` instances worth persisting. Implementations range from
a deterministic fake (for tests) to LLM-backed extraction.
"""

from typing import Protocol

from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.conversation import Message
from kokoro_link.domain.entities.memory_item import MemoryItem


class MemoryExtractorPort(Protocol):
    async def extract(
        self,
        *,
        character: Character,
        conversation_id: str,
        user_message: str,
        assistant_message: str,
        recent_messages: list[Message] | None = None,
    ) -> list[MemoryItem]:
        """Extract structured memories from a completed chat turn.

        Returning an empty list is valid and means the turn did not
        contain anything worth remembering. Implementations should never
        raise on parsing failures — degrade to an empty result instead,
        so a flaky extractor cannot break the chat flow.

        ``recent_messages`` is the dialogue history prior to the current
        turn, used to ground memory extraction in multi-turn context.
        """
