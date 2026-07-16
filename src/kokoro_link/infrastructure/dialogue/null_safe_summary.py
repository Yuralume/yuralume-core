"""Null safe-summary generator used when no LLM is available."""

from __future__ import annotations

from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.contracts.nsfw_safe_summary import NsfwSafeSummaryPort
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.conversation import Message


class NullNsfwSafeSummarizer(NsfwSafeSummaryPort):
    async def summarize(
        self,
        *,
        character: Character,
        message: Message,
        model: ChatModelPort | None = None,
        model_id: str | None = None,
    ) -> str:
        return ""
