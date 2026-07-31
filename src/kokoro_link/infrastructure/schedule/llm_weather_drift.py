"""LLM-backed adapter for :class:`ScheduleWeatherDriftPort`.

Hands the model two things — the schedule prose the character is about
to live out, and the weather that is true *right now* — and asks which
blocks plainly contradict the sky. Per the project's top directive there
is no weather-category table and no "if the description mentions 傘 then
…" branch anywhere in this module: 河堤散步 under a fresh downpour and
a film screening under the same downpour differ only semantically, and
only the model can tell them apart. Python here does prompt assembly and
defensive parsing, nothing else.

Output is a JSON array of per-activity verdicts::

    [{"activity_id": "…", "action": "keep"},
     {"activity_id": "…", "action": "rewrite", "description": "…"}]

Only ``rewrite`` verdicts survive parsing, and only for ids the caller
supplied — everything else (hallucinated ids, invented actions, blank
prose, malformed JSON) degrades to "change nothing", which leaves the
day exactly as planned.
"""

from __future__ import annotations

import logging
from datetime import datetime, tzinfo
from typing import Any

from kokoro_link.application.services.model_resolver import ModelResolver
from kokoro_link.contracts.active_llm import ActiveLLMProviderPort
from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.contracts.schedule_weather_drift import (
    ActivityWeatherRewrite,
    ScheduleWeatherDriftPort,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.schedule import ScheduleActivity
from kokoro_link.infrastructure.llm.cloud_refusal import (
    log_auxiliary_llm_failure,
)
from kokoro_link.infrastructure.memory.json_parser import parse_memory_payload
from kokoro_link.infrastructure.prompt.operator_language import (
    render_operator_language_hint,
)
from kokoro_link.infrastructure.prompts import get_default_loader

_LOGGER = logging.getLogger(__name__)

_REWRITE_ACTION = "rewrite"
"""The only verdict that changes anything. ``keep`` — and any word the
model invents — is a no-op, so an unrecognised action can never delete
or reshape a block."""

_MAX_DESCRIPTION_CHARS = 120
_MAX_CATEGORY_CHARS = 40
_MAX_LOCATION_CHARS = 80
"""Mirrors the planner's field caps so a rewritten block stays the same
shape as the one the planner originally wrote."""


class LLMScheduleWeatherDriftJudge(ScheduleWeatherDriftPort):
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

    async def judge(
        self,
        *,
        character: Character,
        activities: tuple[ScheduleActivity, ...],
        weather_context: str,
        now: datetime,
        local_tz: tzinfo,
        operator_primary_language: str = "zh-TW",
    ) -> tuple[ActivityWeatherRewrite, ...]:
        # Nothing to compare against — an empty window or a deployment
        # without a weather provider. Cost guard only; the semantic call
        # is never made here.
        if not activities or not weather_context.strip():
            return ()
        if await self._resolver.is_fake(character=character):
            return ()
        prompt = _build_prompt(
            character=character,
            activities=activities,
            weather_context=weather_context,
            now=now,
            local_tz=local_tz,
            operator_primary_language=operator_primary_language,
        )
        try:
            raw = await self._resolver.generate(prompt, character=character)
        except Exception as exc:
            log_auxiliary_llm_failure(
                _LOGGER,
                exc,
                "Schedule weather-drift LLM call failed character=%s",
                character.id,
            )
            return ()
        return _parse(
            raw, allowed_ids=frozenset(act.id for act in activities),
        )


class NullScheduleWeatherDriftJudge(ScheduleWeatherDriftPort):
    """Always returns "nothing drifted" — wired when the deployment is
    on the fake provider so the tick keeps its shape without a
    conditional in the container."""

    async def judge(
        self,
        *,
        character: Character,
        activities: tuple[ScheduleActivity, ...],
        weather_context: str,
        now: datetime,
        local_tz: tzinfo,
        operator_primary_language: str = "zh-TW",
    ) -> tuple[ActivityWeatherRewrite, ...]:
        _ = (
            character, activities, weather_context, now, local_tz,
            operator_primary_language,
        )
        return ()


# ----------------------------------------------------------------------
# Prompt rendering
# ----------------------------------------------------------------------


def _build_prompt(
    *,
    character: Character,
    activities: tuple[ScheduleActivity, ...],
    weather_context: str,
    now: datetime,
    local_tz: tzinfo,
    operator_primary_language: str = "zh-TW",
) -> str:
    body = get_default_loader().render(
        "schedule/weather_drift",
        persona_block="\n".join(_persona_block(character)),
        now_text=now.astimezone(local_tz).strftime("%H:%M"),
        weather_block=weather_context.strip(),
        activity_block="\n".join(
            _activity_line(act, local_tz=local_tz) for act in activities
        ),
        max_description_chars=_MAX_DESCRIPTION_CHARS,
    )
    # The rewritten description replaces prose the player reads in the
    # schedule panel and every downstream prompt, so the operator's
    # content language must lead the prompt (bug B2 class).
    language_hint = render_operator_language_hint(operator_primary_language)
    if language_hint:
        return f"{language_hint}\n\n{body}"
    return body


def _persona_block(character: Character) -> list[str]:
    """A deliberately thin persona sketch. The judgement is about the
    sky, not the psyche — the persona is here so a rewrite sounds like
    this character, not so it changes what counts as a contradiction."""
    lines = [f"- 名稱：{character.name}"]
    if character.summary:
        lines.append(f"- 簡介：{character.summary[:160]}")
    if character.personality:
        lines.append("- 性格：" + "、".join(character.personality[:6]))
    if character.interests:
        lines.append("- 興趣：" + "、".join(character.interests[:6]))
    return lines


def _activity_line(activity: ScheduleActivity, *, local_tz: tzinfo) -> str:
    start = activity.start_at.astimezone(local_tz).strftime("%H:%M")
    end = activity.end_at.astimezone(local_tz).strftime("%H:%M")
    location = activity.location or "（未指定地點）"
    return (
        f"- activity_id={activity.id}｜{start}–{end}｜"
        f"描述：{activity.description}｜地點：{location}｜"
        f"類別：{activity.category}｜busy_score={activity.busy_score:.2f}"
    )


# ----------------------------------------------------------------------
# Output parsing
# ----------------------------------------------------------------------


def _parse(
    raw: str, *, allowed_ids: frozenset[str],
) -> tuple[ActivityWeatherRewrite, ...]:
    """Extract the rewrite verdicts from a model response.

    Never raises. ``parse_memory_payload`` already tolerates code
    fences, preambles and trailing commentary and yields an empty list
    for anything that isn't a JSON array of objects.
    """
    rewrites: list[ActivityWeatherRewrite] = []
    seen: set[str] = set()
    for entry in parse_memory_payload(raw):
        activity_id = _coerce_text(entry.get("activity_id"), limit=128)
        if activity_id not in allowed_ids or activity_id in seen:
            # Unknown ids are hallucinations: the caller deliberately
            # kept encounters and user-confirmed promises out of the
            # window, and honouring an invented id would rewrite them.
            continue
        action = _coerce_text(entry.get("action"), limit=32).lower()
        if action != _REWRITE_ACTION:
            continue
        description = _coerce_text(
            entry.get("description"), limit=_MAX_DESCRIPTION_CHARS,
        )
        if not description:
            # A rewrite with no prose would blank the day. Treat it as
            # the keep the model presumably meant.
            continue
        seen.add(activity_id)
        rewrites.append(
            ActivityWeatherRewrite(
                activity_id=activity_id,
                description=description,
                location=_coerce_text(
                    entry.get("location"), limit=_MAX_LOCATION_CHARS,
                ) or None,
                category=_coerce_text(
                    entry.get("category"), limit=_MAX_CATEGORY_CHARS,
                ) or None,
                busy_score=_coerce_busy(entry.get("busy_score")),
            ),
        )
    return tuple(rewrites)


def _coerce_text(raw: Any, *, limit: int) -> str:
    if not isinstance(raw, str):
        return ""
    return raw.strip()[:limit]


def _coerce_busy(raw: Any) -> float | None:
    """Return a clamped busy score, or ``None`` when the field is absent
    or unparseable — ``None`` means "keep the planner's score"."""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return max(0.0, min(1.0, float(raw)))
    if isinstance(raw, str):
        try:
            return max(0.0, min(1.0, float(raw.strip())))
        except ValueError:
            return None
    return None
