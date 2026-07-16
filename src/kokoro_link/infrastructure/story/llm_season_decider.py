"""LLM-backed dormant story-arc season opener decider."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from kokoro_link.application.services.model_resolver import ModelResolver
from kokoro_link.contracts.active_llm import ActiveLLMProviderPort
from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.contracts.story_arc import (
    StoryArcSeasonContext,
    StoryArcSeasonDeciderPort,
    StoryArcSeasonDecision,
)
from kokoro_link.infrastructure.prompts import get_default_loader

_LOGGER = logging.getLogger(__name__)
_FENCE_RE = re.compile(r"```(?:\w+)?\n?")


class NullStoryArcSeasonDecider(StoryArcSeasonDeciderPort):
    async def decide(
        self, context: StoryArcSeasonContext,
    ) -> StoryArcSeasonDecision:
        return StoryArcSeasonDecision(
            should_start=False,
            reason="null story-arc season decider",
        )


class LLMStoryArcSeasonDecider(StoryArcSeasonDeciderPort):
    def __init__(
        self,
        *,
        model: ChatModelPort | None = None,
        provider: ActiveLLMProviderPort | None = None,
        feature_key: str | None = None,
    ) -> None:
        self._resolver = ModelResolver(
            provider=provider, model=model, feature_key=feature_key,
        )

    async def decide(
        self, context: StoryArcSeasonContext,
    ) -> StoryArcSeasonDecision:
        if await self._resolver.is_fake(character=context.character):
            return StoryArcSeasonDecision(
                should_start=False,
                reason="fake provider; keep story arc dormant",
            )
        prompt = _build_prompt(context)
        try:
            raw = await self._resolver.generate(prompt, character=context.character)
        except Exception:
            _LOGGER.exception(
                "story arc season decider LLM call failed character=%s",
                context.character.id,
            )
            return StoryArcSeasonDecision(
                should_start=False,
                reason="season decider LLM call failed",
            )
        parsed = _parse_decision(raw)
        if parsed is None:
            _LOGGER.warning("story arc season decider: unparseable LLM output")
            return StoryArcSeasonDecision(
                should_start=False,
                reason="season decider output was unparseable",
            )
        return parsed


def _build_prompt(context: StoryArcSeasonContext) -> str:
    character = context.character
    completed = context.completed_arc
    completed_block = "（沒有上一段完成故事）"
    if completed is not None:
        completed_block = (
            f"title: {completed.title}\n"
            f"premise: {completed.premise}\n"
            f"theme: {completed.theme}\n"
            f"source_template_id: {completed.source_template_id or 'LLM'}"
        )
    series_block = "（非 series-bound：若開新季，才會交給 LLM planner 規劃內容）"
    if context.series_id:
        series_block = (
            "series-bound: true\n"
            f"series_id: {context.series_id}\n"
            f"series_title: {context.series_title or '（未命名）'}\n"
            f"next_template_id: {context.next_template_id or '（未知）'}\n"
            f"next_template_title: {context.next_template_title or '（未知）'}\n"
            "注意：下一本內容由作者預寫 template 決定。你只判斷現在"
            "是否適合接上下一本，不要改寫、替換或另創下一季。"
        )
    return get_default_loader().render(
        "story/season_decider",
        character_name=character.name,
        character_summary=character.summary or "（未設定）",
        today=context.today.isoformat(),
        days_since_completed=(
            "unknown"
            if context.days_since_completed is None
            else str(context.days_since_completed)
        ),
        completed_block=completed_block,
        series_block=series_block,
        continuation_summary=context.continuation_summary or "（無）",
        recent_dialogue_summary=context.recent_dialogue_summary or "（無）",
    )


def _parse_decision(raw: str) -> StoryArcSeasonDecision | None:
    text = _FENCE_RE.sub("", raw or "").replace("```", "").strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    should_start = data.get("should_start")
    if not isinstance(should_start, bool):
        return None
    reason = _coerce_str(data.get("reason")) or "no reason provided"
    hint = _coerce_str(data.get("hint")) or None
    return StoryArcSeasonDecision(
        should_start=should_start,
        reason=reason[:400],
        hint=hint[:500] if hint else None,
    )


def _coerce_str(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()
