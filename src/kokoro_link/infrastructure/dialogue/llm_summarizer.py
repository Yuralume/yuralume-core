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

**Every rendered turn carries a time anchor.** Until 2026-08-26 the
transcript was a bare ``角色名：內容`` list — ``Message.created_at`` was
read from storage and then dropped on the floor here. Downstream, the
proactive decider was handed a summary with no clock in it at all and,
asked to write about "what you were just talking about", filled the
missing coordinate in itself: a question asked at 07:46 came back at
07:56 described as 「昨天」. Injecting "now" into the *decider* prompt
(which was already happening) cannot fix that — by then the event's own
timestamp is gone. The anchor has to survive this station.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, tzinfo

from kokoro_link.application.services.model_resolver import ModelResolver
from kokoro_link.infrastructure.llm.cloud_refusal import (
    log_auxiliary_llm_failure,
)
from kokoro_link.contracts.active_llm import ActiveLLMProviderPort
from kokoro_link.contracts.dialogue_summarizer import DialogueSummarizerPort
from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.conversation import Message, MessageRole
from kokoro_link.infrastructure.prompt.timing_utils import (
    format_event_time_anchor,
)
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
        self,
        *,
        character: Character,
        messages: list[Message],
        now: datetime | None = None,
        local_tz: tzinfo | None = None,
    ) -> str:
        useful = [m for m in messages if (m.content or "").strip()]
        if len(useful) < _MIN_TURNS_FOR_SUMMARY:
            return ""
        if await self._resolver.is_fake(character=character):
            return ""
        prompt = _build_prompt(
            character=character,
            messages=useful,
            now=resolve_anchor_now(now),
            local_tz=local_tz or timezone.utc,
        )
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


def resolve_anchor_now(now: datetime | None) -> datetime:
    """Fail-soft "now" for time anchoring: caller's instant, else UTC clock.

    A caller with no instant in hand — currently only the story-scene
    side-story builder, which is handed a civil date and nothing else —
    still gets *an* anchor rather than none. An absolute clock off by a
    timezone beats a fabricated date, which is the whole lesson of the
    2026-08-26 incident; the relative 「約 N 分鐘前」 half is unaffected
    either way, since both instants then come from the same UTC clock.
    """
    if now is None:
        return datetime.now(timezone.utc)
    return now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)


def render_dialogue_line(
    character: Character,
    message: Message,
    *,
    now: datetime,
    local_tz: tzinfo,
) -> str:
    """One transcript line: ``[時間錨] 說話者：內容``.

    Shared with the proactive dispatcher's deterministic fresh-tail block
    so the anchor the summariser reads and the anchor the decider reads
    are rendered by the same code — two formatters would drift, and a
    decider that saw two different clocks for the same turn would be
    worse off than one that saw neither.

    ``created_at`` missing (hand-built messages in tests, rows predating
    the column) degrades to the old un-anchored line rather than raising.
    """
    role_label = "使用者" if message.role is MessageRole.USER else character.name
    text = (message.content or "").strip().replace("\n", " ")
    if len(text) > _MAX_CHARS_PER_TURN:
        text = text[:_MAX_CHARS_PER_TURN] + "…"
    anchor = format_event_time_anchor(
        getattr(message, "created_at", None), now, local_tz=local_tz,
    )
    if not anchor:
        return f"{role_label}：{text}"
    return f"[{anchor}] {role_label}：{text}"


def _build_prompt(
    *,
    character: Character,
    messages: list[Message],
    now: datetime,
    local_tz: tzinfo,
) -> str:
    tail = messages[-_MAX_TURNS_IN_PROMPT:]
    transcript = "\n".join(
        render_dialogue_line(character, m, now=now, local_tz=local_tz)
        for m in tail
    )
    return get_default_loader().render(
        "dialogue/summarizer",
        character_name=character.name,
        transcript=transcript,
    )
