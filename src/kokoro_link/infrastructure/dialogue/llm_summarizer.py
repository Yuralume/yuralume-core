"""LLM-backed dialogue summarizer.

Condenses the last N dialogue turns into a compact paragraph so
schedule / arc / proactive prompts can cite "what's going on" without
shipping the full transcript. Tool-only messages are filtered out by the
caller (via ``Conversation.recent_messages(exclude_tool_only=True)``)
before arriving here.

The summarizer is intentionally forgiving:

- Empty / single-turn input → returns ``""`` without calling the model.
- LLM failure → also returns ``""``; downstream planners treat that as
  "no context available" and skip the section.
"""

from __future__ import annotations

import logging

from kokoro_link.application.services.model_resolver import ModelResolver
from kokoro_link.infrastructure.llm.cloud_refusal import (
    log_auxiliary_llm_failure,
)
from kokoro_link.contracts.active_llm import ActiveLLMProviderPort
from kokoro_link.contracts.dialogue_summarizer import DialogueSummarizerPort
from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.conversation import Message, MessageRole
from kokoro_link.infrastructure.prompts import get_default_loader

_LOGGER = logging.getLogger(__name__)

_MIN_TURNS_FOR_SUMMARY = 2
_MAX_TURNS_IN_PROMPT = 30
_MAX_CHARS_PER_TURN = 400
_MAX_SUMMARY_CHARS = 600


class LLMDialogueSummarizer(DialogueSummarizerPort):
    def __init__(
        self,
        model: ChatModelPort | None = None,
        *,
        provider: ActiveLLMProviderPort | None = None,
        feature_key: str | None = None,
    ) -> None:
        self._resolver = ModelResolver(
            provider=provider, model=model, feature_key=feature_key,
        )

    async def summarize(
        self, *, character: Character, messages: list[Message],
    ) -> str:
        useful = [m for m in messages if (m.content or "").strip()]
        if len(useful) < _MIN_TURNS_FOR_SUMMARY:
            return ""
        if await self._resolver.is_fake(character=character):
            return ""
        prompt = _build_prompt(character=character, messages=useful)
        try:
            raw = await self._resolver.generate(prompt, character=character)
        except Exception as exc:
            log_auxiliary_llm_failure(
                _LOGGER, exc, "Dialogue summarizer LLM call failed",
            )
            return ""
        summary = (raw or "").strip()
        if not summary:
            return ""
        return summary[:_MAX_SUMMARY_CHARS]


def _build_prompt(*, character: Character, messages: list[Message]) -> str:
    tail = messages[-_MAX_TURNS_IN_PROMPT:]
    transcript = "\n".join(_format_line(character, m) for m in tail)
    return get_default_loader().render(
        "dialogue/summarizer",
        character_name=character.name,
        transcript=transcript,
    )


def _format_line(character: Character, message: Message) -> str:
    role_label = "使用者" if message.role is MessageRole.USER else character.name
    text = (message.content or "").strip().replace("\n", " ")
    if len(text) > _MAX_CHARS_PER_TURN:
        text = text[:_MAX_CHARS_PER_TURN] + "…"
    return f"{role_label}：{text}"
