"""LLM-backed adapter for :class:`ActivityAftermathPort`.

Asks the model to read a completed activity in light of the character's
persona and produce a short emotional residue line that the memorialiser
folds into the episodic memory's content. The same activity affects
different personas differently because the LLM sees their persona axes
(個性 / 興趣 / 年齡 / 簡介 / 說話風格) and judges accordingly — per the
project's top directive, no enumerated "activity X → emotion Y" rules.

Output is plain text with two labelled lines so parsing stays trivial
and tolerant to model jitter:

::

    情緒尾韻：被同事一直追問感情狀況，很煩
    情緒標籤：煩躁

Missing label → empty field. Empty result → "no notable residue", the
memorialiser falls back to the bare-activity memory.
"""

from __future__ import annotations

import logging
import re

from kokoro_link.application.services.model_resolver import ModelResolver
from kokoro_link.infrastructure.llm.cloud_refusal import (
    log_auxiliary_llm_failure,
)
from kokoro_link.contracts.active_llm import ActiveLLMProviderPort
from kokoro_link.contracts.activity_aftermath import (
    ActivityAftermath,
    ActivityAftermathPort,
)
from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.schedule import ScheduleActivity
from kokoro_link.infrastructure.prompt.operator_language import (
    render_operator_language_hint,
)
from kokoro_link.infrastructure.prompts import get_default_loader

_LOGGER = logging.getLogger(__name__)

_MAX_RESIDUE_CHARS = 120
"""Cap on the residue summary. Sized for the widest shipped language, not
CJK: a Chinese residue is dense (~30-40 chars) but the same thought in
English ("still a bit drained after being grilled about my relationship
status the whole time") needs several times that. The former CJK-sized
60-char cap silently truncated natural-length non-CJK residues mid-word.
Models that overshoot get truncated rather than rejected (some signal
beats none)."""

_MAX_EMOTION_TAG_CHARS = 24
"""Mood tag should be a single short word ("煩躁" / "annoyed" /
"overwhelmed"). Widened from a CJK-sized 6 so a non-CJK tag
("passive-aggressive") isn't dropped for length. Longer than this is
almost certainly the model misreading the schema — a whole sentence."""


class LLMActivityAftermathJudge(ActivityAftermathPort):
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
        activity: ScheduleActivity,
        operator_primary_language: str = "zh-TW",
    ) -> ActivityAftermath:
        if await self._resolver.is_fake(character=character):
            return ActivityAftermath()
        prompt = _build_prompt(
            character=character,
            activity=activity,
            operator_primary_language=operator_primary_language,
        )
        try:
            raw = await self._resolver.generate(prompt, character=character)
        except Exception:
            _LOGGER.exception(
                "aftermath LLM call failed character=%s activity=%s",
                character.id, activity.id,
            )
            return ActivityAftermath()
        return _parse(raw)


class NullActivityAftermathJudge(ActivityAftermathPort):
    """Always returns blank — used when the deployment is on the fake
    provider so the memorialiser degrades to bare-activity memories
    without any conditional in the container."""

    async def judge(
        self,
        *,
        character: Character,
        activity: ScheduleActivity,
        operator_primary_language: str = "zh-TW",
    ) -> ActivityAftermath:
        _ = operator_primary_language
        return ActivityAftermath()


# ----------------------------------------------------------------------
# Prompt rendering
# ----------------------------------------------------------------------


def _build_prompt(
    *,
    character: Character,
    activity: ScheduleActivity,
    operator_primary_language: str = "zh-TW",
) -> str:
    companions = (
        "、".join(activity.companion_names)
        if activity.companion_names else "（獨自進行）"
    )
    return get_default_loader().render(
        "schedule/aftermath",
        # The 情緒尾韻 residue is folded verbatim into the episodic memory
        # content shown in MemoryBrowserPanel, so it must follow the
        # operator's content language (bug B2 class).
        language_hint=render_operator_language_hint(operator_primary_language),
        persona_block="\n".join(_persona_block(character)),
        activity_description=activity.description,
        activity_category=activity.category,
        activity_location=activity.location or "（未指定地點）",
        activity_companions=companions,
        busy_hint=_busy_label(activity.busy_score),
        activity_busy_score=f"{activity.busy_score:.2f}",
        max_residue_chars=_MAX_RESIDUE_CHARS,
        max_emotion_tag_chars=_MAX_EMOTION_TAG_CHARS,
    )


def _persona_block(character: Character) -> list[str]:
    lines = [f"- 名稱：{character.name}"]
    if character.summary:
        lines.append(f"- 簡介：{character.summary[:160]}")
    if character.personality:
        lines.append("- 性格：" + "、".join(character.personality[:6]))
    if character.interests:
        lines.append("- 興趣：" + "、".join(character.interests[:6]))
    if character.speaking_style:
        lines.append(f"- 說話風格：{character.speaking_style[:120]}")
    state = character.state
    lines.append(
        f"- 當前情緒：{state.emotion}（好感 {state.affection}/疲勞 "
        f"{state.fatigue}/精力 {state.energy}）",
    )
    return lines


def _busy_label(score: float) -> str:
    if score >= 0.8:
        return "高度忙碌（強度大）"
    if score >= 0.5:
        return "中等忙碌"
    if score >= 0.2:
        return "輕度忙碌"
    return "幾乎沒在動"


# ----------------------------------------------------------------------
# Output parsing
# ----------------------------------------------------------------------


_RESIDUE_RE = re.compile(r"情緒尾韻\s*[:：]\s*(.*)")
_EMOTION_RE = re.compile(r"情緒標籤\s*[:：]\s*(.*)")
_FENCE_RE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")


def _parse(raw: str) -> ActivityAftermath:
    text = (raw or "").strip()
    if not text:
        return ActivityAftermath()
    text = _FENCE_RE.sub("", text).strip()
    residue = ""
    emotion = ""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        m = _RESIDUE_RE.match(stripped)
        if m:
            residue = m.group(1).strip()
            continue
        m = _EMOTION_RE.match(stripped)
        if m:
            emotion = m.group(1).strip()
    residue = _clean_residue(residue)
    emotion = _clean_emotion(emotion)
    return ActivityAftermath(residue_summary=residue, emotion_tag=emotion)


def _clean_residue(text: str) -> str:
    cleaned = text.strip().strip('「」"\'')
    if not cleaned:
        return ""
    if len(cleaned) > _MAX_RESIDUE_CHARS:
        cleaned = cleaned[:_MAX_RESIDUE_CHARS].rstrip() + "…"
    return cleaned


def _clean_emotion(text: str) -> str:
    cleaned = text.strip().strip('「」"\'')
    if not cleaned:
        return ""
    if len(cleaned) > _MAX_EMOTION_TAG_CHARS:
        # Model overshot — likely a sentence instead of a label. Drop
        # rather than truncate; an emotion tag of "被同事煩到頭痛" would
        # poison the memory tags downstream.
        return ""
    return cleaned
