"""LLM-backed self-repetition extractor.

Reads the character's last several assistant turns in this
conversation and asks the LLM to name what's started repeating —
topics, openings, phrasings, metaphors. Output is short prose, not
structured findings, because the consumer is a prompt rail that
inlines the hint verbatim ("最近你重複了X — 本輪請避開"). One LLM
call total, fire-and-forget from the chat finalizer.

Returns empty string on any failure path (fake provider, exception,
empty output) so the caller's "skip rail" branch handles it. The
character argument is plumbed through to ``ModelResolver`` so the
per-character LLM routing (``feature_models[chat_repetition_check]``
override) takes effect.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from kokoro_link.application.services.model_resolver import ModelResolver
from kokoro_link.contracts.active_llm import ActiveLLMProviderPort
from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.contracts.self_repetition import SelfRepetitionExtractorPort
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.conversation import Message
from kokoro_link.infrastructure.prompts import get_default_loader


_LOGGER = logging.getLogger(__name__)

_PER_TURN_SNIPPET = 240
"""Maximum sampled characters per assistant turn."""

_PER_TURN_HEAD_SNIPPET = 140
_PER_TURN_TAIL_SNIPPET = 100
"""Long turns are sampled from both ends so closing habits stay visible."""

_HINT_MAX_CHARS = 600
"""Cap on the returned hint. The chat prompt has a strict context
budget; an over-long hint would push out memory / history rails. The
extractor is told to stay terse — this cap is the backstop."""


class LLMSelfRepetitionExtractor(SelfRepetitionExtractorPort):
    def __init__(
        self,
        *,
        provider: ActiveLLMProviderPort | None = None,
        model: ChatModelPort | None = None,
        feature_key: str | None = None,
    ) -> None:
        self._resolver = ModelResolver(
            provider=provider, model=model, feature_key=feature_key,
        )

    async def extract(
        self,
        *,
        character: Character,
        recent_assistant_messages: Sequence[Message],
    ) -> str:
        if not recent_assistant_messages:
            return ""
        if await self._resolver.is_fake(character=character):
            return ""

        prompt = _build_prompt(
            character_name=character.name,
            assistant_messages=recent_assistant_messages,
        )
        try:
            raw = await self._resolver.generate(prompt, character=character)
        except Exception:
            _LOGGER.exception("self-repetition extractor LLM call failed")
            return ""
        cleaned = (raw or "").strip()
        if not cleaned:
            return ""
        # Reject "nothing to flag" sentinels the model sometimes emits
        # instead of returning an empty body. Loose match — any of
        # these prefixes means the model judged the rail unnecessary.
        lowered = cleaned.lower()
        if (
            lowered.startswith("無")
            or lowered.startswith("沒有")
            or lowered.startswith("none")
            or lowered.startswith("n/a")
        ):
            return ""
        if len(cleaned) > _HINT_MAX_CHARS:
            cleaned = cleaned[:_HINT_MAX_CHARS].rstrip() + "…"
        return cleaned


def _build_prompt(
    *,
    character_name: str,
    assistant_messages: Sequence[Message],
) -> str:
    lines: list[str] = []
    for idx, msg in enumerate(assistant_messages, start=1):
        text = msg.content.strip().replace("\n", " ")
        lines.append(f"{idx}. {_sample_turn_text(text)}")
    transcript = "\n".join(lines) or "（無內容）"
    return get_default_loader().render(
        "self_repetition/extractor",
        character_name=character_name,
        transcript=transcript,
    )


def _sample_turn_text(text: str) -> str:
    if len(text) <= _PER_TURN_SNIPPET:
        return text
    head = text[:_PER_TURN_HEAD_SNIPPET].rstrip()
    tail = text[-_PER_TURN_TAIL_SNIPPET:].lstrip()
    return f"{head}……{tail}"
