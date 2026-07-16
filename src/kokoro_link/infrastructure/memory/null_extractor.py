"""No-op memory extractor used when no real LLM is configured.

Prefer wiring ``LLMMemoryExtractor`` with a real provider in production.
This implementation exists so the chat flow still runs in fake-only dev
setups without writing nonsensical "memories" extracted from a mock.
"""

from kokoro_link.contracts.memory_extractor import MemoryExtractorPort
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.conversation import Message
from kokoro_link.domain.entities.memory_item import MemoryItem


class NullMemoryExtractor(MemoryExtractorPort):
    async def extract(
        self,
        *,
        character: Character,
        conversation_id: str,
        user_message: str,
        assistant_message: str,
        recent_messages: list[Message] | None = None,
    ) -> list[MemoryItem]:
        _ = (character, conversation_id, user_message, assistant_message, recent_messages)
        return []
