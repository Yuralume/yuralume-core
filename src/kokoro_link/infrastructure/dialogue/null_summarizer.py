"""Null dialogue summarizer.

Returns empty string unconditionally — used by the fake provider / tests
where we don't want to spend an LLM call on conversation compression.
Downstream planners treat empty as "no dialogue context" and skip the
relevant prompt section, so the system keeps working end-to-end.
"""

from __future__ import annotations

from kokoro_link.contracts.dialogue_summarizer import DialogueSummarizerPort
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.conversation import Message


class NullDialogueSummarizer(DialogueSummarizerPort):
    async def summarize(
        self, *, character: Character, messages: list[Message],
    ) -> str:
        return ""
