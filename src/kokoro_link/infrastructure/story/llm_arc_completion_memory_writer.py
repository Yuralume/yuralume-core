"""LLM-backed relationship milestone writer for completed arcs."""

from __future__ import annotations

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
from kokoro_link.infrastructure.story.date_context import (
    render_story_date_context_block,
)
from kokoro_link.llm_output import ParseReason, extract_object_outcome, log_parse_outcome


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
        # CF1b: a relationship milestone is read back for months, so a
        # relative time word frozen into it never resolves.
        date_context_block=render_story_date_context_block(
            context.today,
            language_tag=context.operator_primary_language,
        ),
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
    outcome = extract_object_outcome(text, repair_truncated=True)
    if outcome.value is not None:
        content = outcome.value.get("content")
        if isinstance(content, str):
            return _clean(content)
    elif outcome.reason is not ParseReason.NO_JSON:
        # A '{' was found but nothing usable came out of it (versus plain
        # prose, which is this site's legitimate other reply shape and
        # not worth a warning) — log once for visibility.
        log_parse_outcome(_LOGGER, outcome, site="story.arc_completion_memory_writer")
    # Same guard as before the migration: a region that looks like a
    # broken JSON envelope (starts with '{' or ends with '}') must not
    # leak as narration text even when it never resolved to usable
    # content — only text that never looked JSON-shaped falls through
    # to the plain-prose path below.
    if text.startswith("{") or text.endswith("}"):
        return ""
    return _clean(text)


def _clean(value: str) -> str:
    text = re.sub(r"\s+", " ", value.strip())
    if len(text) > _MAX_CONTENT_CHARS:
        text = text[:_MAX_CONTENT_CHARS].rstrip() + "…"
    return text
