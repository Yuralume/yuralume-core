"""LLM-backed relationship milestone writer for completed arcs."""

from __future__ import annotations

import json
import logging
import re

from kokoro_link.application.services.model_resolver import ModelResolver
from kokoro_link.contracts.active_llm import ActiveLLMProviderPort
from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.contracts.story_arc import (
    ArcCompletionMemoryContext,
    ArcCompletionMemoryDraft,
    ArcCompletionMemoryWriterPort,
)
from kokoro_link.infrastructure.localization.fallback_texts import (
    localized_fallback_text,
)
from kokoro_link.infrastructure.prompt.operator_language import (
    render_operator_language_hint,
)
from kokoro_link.infrastructure.prompts import get_default_loader


_LOGGER = logging.getLogger(__name__)
_MAX_CONTENT_CHARS = 800


class NullArcCompletionMemoryWriter(ArcCompletionMemoryWriterPort):
    async def write_memory(
        self,
        context: ArcCompletionMemoryContext,
    ) -> ArcCompletionMemoryDraft:
        summary = "；".join(
            f"{beat.title}：{beat.summary}"
            for beat in context.realized_beats[-3:]
        )
        content = localized_fallback_text(
            "memory.arc_completion_fallback",
            context.operator_primary_language,
            title=context.arc.title,
            summary=summary,
        )
        return ArcCompletionMemoryDraft(content=content)


class LLMArcCompletionMemoryWriter(ArcCompletionMemoryWriterPort):
    def __init__(
        self,
        *,
        model: ChatModelPort | None = None,
        provider: ActiveLLMProviderPort | None = None,
        feature_key: str | None = None,
    ) -> None:
        self._resolver = ModelResolver(
            provider=provider,
            model=model,
            feature_key=feature_key,
        )

    async def write_memory(
        self,
        context: ArcCompletionMemoryContext,
    ) -> ArcCompletionMemoryDraft:
        async def _fallback() -> ArcCompletionMemoryDraft:
            return await NullArcCompletionMemoryWriter().write_memory(context)

        if await self._resolver.is_fake(character=context.character):
            return await _fallback()
        prompt = _build_prompt(context)
        try:
            raw = await self._resolver.generate(
                prompt,
                character=context.character,
            )
        except Exception:
            _LOGGER.exception(
                "arc completion memory writer LLM call failed arc=%s",
                context.arc.id,
            )
            return await _fallback()
        content = _parse_content(raw)
        if not content:
            return await _fallback()
        return ArcCompletionMemoryDraft(content=content)


def _build_prompt(context: ArcCompletionMemoryContext) -> str:
    beat_lines = []
    for beat in context.realized_beats:
        beat_lines.append(
            "- "
            f"{beat.scheduled_date.isoformat()} | {beat.tension} | "
            f"{beat.title}: {beat.summary}",
        )
    body = get_default_loader().render(
        "story/arc_completion_memory",
        character_name=context.character.name,
        character_summary=context.character.summary or "（未設定）",
        arc_title=context.arc.title,
        arc_premise=context.arc.premise,
        arc_theme=context.arc.theme,
        realized_beat_block="\n".join(beat_lines) or "（無）",
    )
    language_hint = render_operator_language_hint(
        context.operator_primary_language,
    )
    return f"{language_hint}\n\n{body}" if language_hint else body


def _parse_content(raw: str) -> str:
    text = (raw or "").strip()
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict):
            content = data.get("content")
            if isinstance(content, str):
                return _clean(content)
    if text.startswith("{") or text.endswith("}"):
        return ""
    return _clean(text)


def _clean(value: str) -> str:
    text = re.sub(r"\s+", " ", value.strip())
    if len(text) > _MAX_CONTENT_CHARS:
        text = text[:_MAX_CONTENT_CHARS].rstrip() + "…"
    return text
