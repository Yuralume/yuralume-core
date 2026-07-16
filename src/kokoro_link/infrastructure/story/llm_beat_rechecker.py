"""LLM-backed recheck for repeatedly surfaced story-arc beats."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from kokoro_link.application.services.model_resolver import ModelResolver
from kokoro_link.contracts.active_llm import ActiveLLMProviderPort
from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.contracts.story_arc import (
    StoryBeatRecheckContext,
    StoryBeatRecheckDecision,
    StoryBeatRecheckerPort,
)
from kokoro_link.infrastructure.prompt.operator_language import (
    render_operator_language_hint,
)
from kokoro_link.infrastructure.prompts import get_default_loader


_LOGGER = logging.getLogger(__name__)
_FENCE_RE = re.compile(r"```(?:\w+)?\n?")
_VALID_ACTIONS = {
    "keep_pending",
    "delay_beat",
    "skip_beat",
    "mark_realized",
}


class NullStoryBeatRechecker(StoryBeatRecheckerPort):
    async def recheck(
        self,
        context: StoryBeatRecheckContext,
    ) -> StoryBeatRecheckDecision:
        return StoryBeatRecheckDecision(
            action="keep_pending",
            reason="null story-beat rechecker",
        )


class LLMStoryBeatRechecker(StoryBeatRecheckerPort):
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

    async def recheck(
        self,
        context: StoryBeatRecheckContext,
    ) -> StoryBeatRecheckDecision:
        if await self._resolver.is_fake(character=context.character):
            return await NullStoryBeatRechecker().recheck(context)
        prompt = _build_prompt(context)
        try:
            raw = await self._resolver.generate(
                prompt,
                character=context.character,
            )
        except Exception:
            _LOGGER.exception(
                "story beat rechecker LLM call failed beat=%s",
                context.beat.id,
            )
            return await NullStoryBeatRechecker().recheck(context)
        parsed = _parse_decision(raw)
        if parsed is None:
            return await NullStoryBeatRechecker().recheck(context)
        return parsed


def _build_prompt(context: StoryBeatRecheckContext) -> str:
    beat = context.beat
    attempt_block = "\n".join(
        [
            f"- play_attempt_count: {beat.play_attempt_count}",
            f"- last_play_attempt_at: {beat.last_play_attempt_at.isoformat() if beat.last_play_attempt_at else 'none'}",
            f"- last_play_attempt_source: {beat.last_play_attempt_source or 'none'}",
            f"- last_play_attempt_result: {beat.last_play_attempt_result or 'none'}",
            f"- last_play_push_intensity: {beat.last_play_push_intensity or 'none'}",
        ],
    )
    body = get_default_loader().render(
        "story/beat_rechecker",
        character_name=context.character.name,
        character_summary=context.character.summary or "（未設定）",
        today=context.today.isoformat(),
        arc_title=context.arc.title,
        arc_premise=context.arc.premise,
        beat_title=beat.title,
        beat_summary=beat.summary,
        beat_tension=beat.tension,
        beat_scheduled_date=beat.scheduled_date.isoformat(),
        beat_required="是" if beat.required else "否",
        attempt_block=attempt_block,
        recent_dialogue_summary=(
            context.recent_dialogue_summary.strip() or "（無）"
        ),
    )
    language_hint = render_operator_language_hint(
        context.operator_primary_language,
    )
    return f"{language_hint}\n\n{body}" if language_hint else body


def _parse_decision(raw: str) -> StoryBeatRecheckDecision | None:
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
    action = _coerce_str(data.get("action"))
    if action not in _VALID_ACTIONS:
        return None
    days = _coerce_positive_int(data.get("days"))
    narrative = _coerce_str(data.get("narrative")) or None
    if action == "mark_realized" and not narrative:
        return None
    return StoryBeatRecheckDecision(
        action=action,
        reason=_coerce_str(data.get("reason"))[:400],
        days=days,
        narrative=narrative[:1200] if narrative else None,
    )


def _coerce_str(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value.strip())


def _coerce_positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float):
        candidate = int(value)
        return candidate if candidate > 0 else None
    if isinstance(value, str):
        try:
            candidate = int(value.strip())
        except ValueError:
            return None
        return candidate if candidate > 0 else None
    return None
