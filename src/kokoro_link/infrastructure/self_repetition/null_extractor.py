"""No-op self-repetition extractor.

Used in test rigs and any boot where the operator has no LLM
configured but the chat path still needs a port to call. Always
returns an empty hint so the caller's "no hint → skip rail" branch
kicks in and nothing changes about the prompt.
"""

from __future__ import annotations

from collections.abc import Sequence

from kokoro_link.contracts.self_repetition import SelfRepetitionExtractorPort
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.conversation import Message


class NullSelfRepetitionExtractor(SelfRepetitionExtractorPort):
    async def extract(
        self,
        *,
        character: Character,
        recent_assistant_messages: Sequence[Message],
    ) -> str:
        return ""
