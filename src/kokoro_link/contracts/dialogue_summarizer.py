"""Dialogue summarizer port.

Schedule / arc / proactive generators want *context* from recent chat
without paying the full token cost of raw turns. The summarizer runs a
dedicated LLM pass that condenses the last N messages into a short
narrative blurb (who said what, current emotional state, open threads)
which downstream planners drop into their prompts.

Running a separate LLM pass is deliberate — it keeps downstream prompts
short and cheap while isolating "context compression" as its own
concern. Implementations may cache per-conversation for a short window
if call volume becomes an issue.
"""

from __future__ import annotations

from typing import Protocol

from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.conversation import Message


class DialogueSummarizerPort(Protocol):
    async def summarize(
        self,
        *,
        character: Character,
        messages: list[Message],
    ) -> str:
        """Condense ``messages`` into a short Traditional-Chinese blurb.

        Returns an empty string when the conversation is too short to be
        worth summarising, or when the underlying LLM call fails — the
        caller treats empty as "no dialogue context available" and
        skips the prompt section entirely.
        """
