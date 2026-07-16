"""LLM-backed adapter for :class:`IdleDriftPort`.

Asks the model to read a character's persona + idle duration and judge
how their mood has shifted while the user was away. Same absence
length yields different drift per persona — that's the whole point —
so the LLM sees the persona axes (個性 / 興趣 / 年齡 / 簡介 / 說話風格 /
當前狀態) and the absence in hours, and is explicitly instructed to
let personality dictate direction.

Output is plain text labels — same shape as the activity-aftermath
adapter — so parsing stays trivial and tolerant to model jitter:

::

    情緒：鬧彆扭
    好感變化：-3
    精力變化：0
    疲勞變化：0
    內心：三天都沒理我，假裝不在意但其實有點生氣
    短期意圖：等對方先低頭

Missing field → unchanged. Out-of-range delta → clamped. Garbage → empty
drift, caller proceeds as if no drift happened. Per the project's top
directive, the prompt never enumerates "if personality=X then emotion=Y";
the LLM owns the judgement.
"""

from __future__ import annotations

import logging
import re

from kokoro_link.application.services.model_resolver import ModelResolver
from kokoro_link.contracts.active_llm import ActiveLLMProviderPort
from kokoro_link.contracts.idle_drift import IdleDrift, IdleDriftPort
from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.domain.entities.character import Character
from kokoro_link.infrastructure.prompt.operator_language import (
    render_operator_language_hint,
)
from kokoro_link.infrastructure.prompts import get_default_loader

_LOGGER = logging.getLogger(__name__)

# Numeric drift is intentionally small — idle drift is a mood nudge,
# not a punishment system. A clingy character left for a week loses at
# most ~8 affection, recoverable in one or two warm exchanges. Keeps
# the mechanic forgiving and reversible.
_MAX_AFFECTION_DELTA = 8
_MAX_FATIGUE_DELTA = 5
_MAX_ENERGY_DELTA = 5

_MAX_EMOTION_CHARS = 24
"""Emotion override must be a short label ("鬧彆扭" / "sulking" /
"quietly hurt"). The cap is sized for the widest shipped language, not
CJK: a Chinese label is ~2-4 chars but an English one ("passive-
aggressive") easily runs past 6, so a CJK-sized cap silently dropped
every non-CJK emotion word. Longer than this = the model wrote a whole
sentence and misread the schema — drop rather than truncate."""

_MAX_INTENT_CHARS = 120
"""Player-visible short-term intent. CJK phrasing is dense (~40 chars);
English ("give them space and wait for them to reach out first") needs
several times that, so the cap is widened for non-CJK output."""

_MAX_NOTE_CHARS = 120


class LLMIdleDriftJudge(IdleDriftPort):
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
        idle_minutes: float,
        operator_primary_language: str = "zh-TW",
    ) -> IdleDrift:
        if await self._resolver.is_fake(character=character):
            return IdleDrift()
        prompt = _build_prompt(
            character=character,
            idle_minutes=idle_minutes,
            operator_primary_language=operator_primary_language,
        )
        try:
            raw = await self._resolver.generate(prompt, character=character)
        except Exception:
            _LOGGER.exception(
                "idle-drift LLM call failed character=%s idle_min=%.1f",
                character.id, idle_minutes,
            )
            return IdleDrift()
        return _parse(raw)


class NullIdleDriftJudge(IdleDriftPort):
    """Always returns empty drift — used when the deployment is on the
    fake provider so chat degrades to a normal turn without any
    conditional in the container."""

    async def judge(
        self,
        *,
        character: Character,
        idle_minutes: float,
        operator_primary_language: str = "zh-TW",
    ) -> IdleDrift:
        return IdleDrift()


# ----------------------------------------------------------------------
# Prompt rendering
# ----------------------------------------------------------------------


def _build_prompt(
    *,
    character: Character,
    idle_minutes: float,
    operator_primary_language: str = "zh-TW",
) -> str:
    return get_default_loader().render(
        "state/idle_drift",
        # ``內心`` and ``短期意圖`` become the player-visible current_intent,
        # so the operator-language fact must ride along.
        language_hint=render_operator_language_hint(operator_primary_language),
        persona_block="\n".join(_persona_block(character)),
        duration=_humanize_duration(idle_minutes),
        idle_minutes=f"{idle_minutes:.0f}",
        max_emotion_chars=_MAX_EMOTION_CHARS,
        max_affection_delta=_MAX_AFFECTION_DELTA,
        max_energy_delta=_MAX_ENERGY_DELTA,
        max_fatigue_delta=_MAX_FATIGUE_DELTA,
        max_note_chars=_MAX_NOTE_CHARS,
        max_intent_chars=_MAX_INTENT_CHARS,
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


def _humanize_duration(minutes: float) -> str:
    if minutes < 60:
        return f"{minutes:.0f} 分鐘"
    hours = minutes / 60.0
    if hours < 24:
        return f"{hours:.1f} 小時"
    days = hours / 24.0
    return f"{days:.1f} 天"


# ----------------------------------------------------------------------
# Output parsing
# ----------------------------------------------------------------------


_EMOTION_RE = re.compile(r"情緒\s*[:：]\s*(.*)")
_AFFECTION_RE = re.compile(r"好感變化\s*[:：]\s*(.*)")
_ENERGY_RE = re.compile(r"精力變化\s*[:：]\s*(.*)")
_FATIGUE_RE = re.compile(r"疲勞變化\s*[:：]\s*(.*)")
_NOTE_RE = re.compile(r"內心\s*[:：]\s*(.*)")
_INTENT_RE = re.compile(r"短期意圖\s*[:：]\s*(.*)")
_FENCE_RE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")
_INT_RE = re.compile(r"-?\d+")


def _parse(raw: str) -> IdleDrift:
    text = (raw or "").strip()
    if not text:
        return IdleDrift()
    text = _FENCE_RE.sub("", text).strip()
    emotion = ""
    affection = 0
    energy = 0
    fatigue = 0
    note = ""
    intent = ""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        m = _EMOTION_RE.match(stripped)
        if m:
            emotion = m.group(1).strip()
            continue
        m = _AFFECTION_RE.match(stripped)
        if m:
            affection = _parse_int(m.group(1), _MAX_AFFECTION_DELTA)
            continue
        m = _ENERGY_RE.match(stripped)
        if m:
            energy = _parse_int(m.group(1), _MAX_ENERGY_DELTA)
            continue
        m = _FATIGUE_RE.match(stripped)
        if m:
            fatigue = _parse_int(m.group(1), _MAX_FATIGUE_DELTA)
            continue
        m = _NOTE_RE.match(stripped)
        if m:
            note = m.group(1).strip()
            continue
        m = _INTENT_RE.match(stripped)
        if m:
            intent = m.group(1).strip()
    return IdleDrift(
        emotion=_clean_emotion(emotion),
        affection_delta=affection,
        energy_delta=energy,
        fatigue_delta=fatigue,
        note=_clean_text(note, _MAX_NOTE_CHARS),
        current_intent=_clean_intent(intent),
    )


def _parse_int(text: str, cap: int) -> int:
    cleaned = text.strip().strip('「」"\'')
    if not cleaned or cleaned in ("0", "+0", "-0"):
        return 0
    m = _INT_RE.search(cleaned)
    if not m:
        return 0
    try:
        value = int(m.group(0))
    except ValueError:
        return 0
    if value > cap:
        return cap
    if value < -cap:
        return -cap
    return value


def _clean_emotion(text: str) -> str | None:
    cleaned = text.strip().strip('「」"\'')
    if not cleaned:
        return None
    if len(cleaned) > _MAX_EMOTION_CHARS:
        # Model overshot — likely wrote a sentence. Drop rather than
        # truncate so we don't store "三天沒理我所以很煩" as an emotion.
        return None
    return cleaned


def _clean_intent(text: str) -> str | None:
    cleaned = _clean_text(text, _MAX_INTENT_CHARS)
    return cleaned or None


def _clean_text(text: str, limit: int) -> str:
    cleaned = text.strip().strip('「」"\'')
    if not cleaned:
        return ""
    if len(cleaned) > limit:
        cleaned = cleaned[:limit].rstrip() + "…"
    return cleaned
