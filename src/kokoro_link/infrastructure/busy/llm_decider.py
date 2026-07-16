"""LLM-backed :class:`BusyReplyDeciderPort`.

Asks the model to read persona + current activity + the user's message
and write its own call: reply normally, or send a brief acknowledgement
now and pick up the actual reply when free.

Per the project's top directive, the prompt never enumerates "if
busy_score > X defer" or "if category in {sleep, meeting} defer". The
LLM owns the judgement — same persona + same activity + different
incoming message should yield different decisions because urgency, tone
and rapport are all read together.

Output is plain Chinese label lines so parsing tolerates jitter:

::

    模式：延後
    短回覆：先回，會議結束我再好好回你
    延後到：18:30
    原因：跟客戶開會

``模式`` only ``立即`` / ``延後`` are accepted — anything else → ``立即``
(fail-soft to "no defer"). ``短回覆`` is required when ``延後``, blank
falls back to ``立即``. ``延後到`` accepts HH:MM (today local), an ISO
datetime, or empty (caller defaults to current activity end).
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, time, timedelta, timezone, tzinfo

from kokoro_link.application.services.model_resolver import ModelResolver
from kokoro_link.contracts.active_llm import ActiveLLMProviderPort
from kokoro_link.contracts.busy_reply_decider import (
    BusyDecision,
    BusyReplyDeciderPort,
    BusyReplyMode,
)
from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.proactive_attempt import ProactiveAttempt
from kokoro_link.domain.entities.schedule import ScheduleActivity
from kokoro_link.infrastructure.prompt.character_identity import (
    render_character_identity_lines,
)
from kokoro_link.infrastructure.prompt.operator_language import (
    render_operator_language_hint,
)
from kokoro_link.infrastructure.prompt.state_tone import (
    affection_tone as _affection_tone,
    energy_tone as _energy_tone,
    fatigue_tone as _fatigue_tone,
    trust_tone as _trust_tone,
)
from kokoro_link.infrastructure.prompts import get_default_loader

_LOGGER = logging.getLogger(__name__)

_MAX_BRIEF_REPLY_CHARS = 160
"""Brief replies are meant to be short IM-style acks ("等我下班"), not
mini-essays. The cap is sized for the widest shipped language, not CJK:
a Chinese ack is ~10-20 chars but an English one ("Sorry, I'm in a
meeting — I'll get back to you properly once it wraps up") easily runs
2-3x that, so the former CJK-sized 80-char cap silently dropped every
natural-length non-CJK ack. Longer than this = the model wrote the full
reply and misread the schema — drop the defer entirely (caller falls
back to ``IMMEDIATE``) rather than persist a misshapen ack."""

_MAX_REASON_CHARS = 48
"""Short telemetry / follow-up label ("會議中" / "in a meeting"). Widened
from a CJK-sized 24 so an English label isn't truncated mid-word."""

_RECENT_OUTREACH_RENDER_LIMIT = 2
"""How many of the character's own most-recent proactive pushes to
surface. The block exists only to let the decider tell "the user is
replying to outreach I just initiated" from "an unsolicited interruption
mid-focus" — one or two newest lines carry that; more is noise."""

_OUTREACH_EXCERPT_CHARS = 80


class LLMBusyReplyDecider(BusyReplyDeciderPort):
    def __init__(
        self,
        model: ChatModelPort | None = None,
        *,
        provider: ActiveLLMProviderPort | None = None,
        feature_key: str | None = None,
        local_tz: tzinfo | None = None,
    ) -> None:
        self._resolver = ModelResolver(
            provider=provider, model=model, feature_key=feature_key,
        )
        self._local_tz = local_tz

    async def decide(
        self,
        *,
        character: Character,
        user_message: str,
        current_activity: ScheduleActivity | None,
        recent_dialogue_summary: str | None = None,
        recent_proactive_attempts: tuple[ProactiveAttempt, ...] = (),
        relationship_context_lines: tuple[str, ...] = (),
        interaction_context_lines: tuple[str, ...] = (),
        now: datetime,
        local_tz: tzinfo | None = None,
        operator_primary_language: str = "zh-TW",
    ) -> BusyDecision:
        message = (user_message or "").strip()
        if not message:
            return BusyDecision()
        if await self._resolver.is_fake(character=character):
            return BusyDecision()
        effective_tz = local_tz or self._local_tz
        prompt = _build_prompt(
            character=character,
            user_message=message,
            current_activity=current_activity,
            recent_dialogue_summary=recent_dialogue_summary,
            recent_proactive_attempts=recent_proactive_attempts,
            relationship_context_lines=relationship_context_lines,
            interaction_context_lines=interaction_context_lines,
            now=now,
            local_tz=effective_tz,
            operator_primary_language=operator_primary_language,
        )
        try:
            raw = await self._resolver.generate(prompt, character=character)
        except Exception:
            _LOGGER.exception(
                "busy-reply decider LLM call failed character=%s",
                character.id,
            )
            return BusyDecision()
        return _parse(
            raw,
            now=now,
            current_activity=current_activity,
            local_tz=effective_tz,
        )


# ----------------------------------------------------------------------
# Prompt rendering
# ----------------------------------------------------------------------


def _build_prompt(
    *,
    character: Character,
    user_message: str,
    current_activity: ScheduleActivity | None,
    recent_dialogue_summary: str | None,
    recent_proactive_attempts: tuple[ProactiveAttempt, ...],
    relationship_context_lines: tuple[str, ...],
    interaction_context_lines: tuple[str, ...],
    now: datetime,
    local_tz: tzinfo | None,
    operator_primary_language: str = "zh-TW",
) -> str:
    persona = "\n".join(
        _persona_block(
            character,
            relationship_context_lines=relationship_context_lines,
            interaction_context_lines=interaction_context_lines,
        ),
    )
    activity = "\n".join(_activity_block(current_activity, now=now, local_tz=local_tz))
    summary = (recent_dialogue_summary or "").strip()
    summary_block = ""
    if summary:
        summary_block = "\n\n最近對話脈絡：\n" + summary[:300]
    outreach_block = _proactive_outreach_block(
        recent_proactive_attempts, now=now,
    )
    return get_default_loader().render(
        "busy/decider",
        # The 短回覆 (brief_reply) ack is sent straight to the player in
        # chat, so it must follow the operator's content language — same
        # fact injected across every other player-visible LLM job.
        language_hint=render_operator_language_hint(operator_primary_language),
        persona_block=persona,
        activity_block=activity,
        summary_block=summary_block,
        proactive_outreach_block=outreach_block,
        user_message=user_message[:400],
    )


def _proactive_outreach_block(
    attempts: tuple[ProactiveAttempt, ...],
    *,
    now: datetime,
) -> str:
    """Render the character's own recent proactive pushes as fact lines.

    Empty string when there are none. Each line carries how long ago the
    push went out so the decider can judge whether the incoming user
    message is a reply to outreach the character just initiated — in
    which case deferring again contradicts the fact that it just chose
    to spend attention on the user. The block states facts only; the
    judgement rule lives in the prompt template (``busy/decider``)."""
    lines: list[str] = []
    for attempt in attempts[:_RECENT_OUTREACH_RENDER_LIMIT]:
        decided = attempt.decided_at
        if decided.tzinfo is None:
            decided = decided.replace(tzinfo=timezone.utc)
        elapsed_min = max(0, int((now - decided).total_seconds() / 60.0))
        text = " ".join((attempt.message or "").split())
        if len(text) > _OUTREACH_EXCERPT_CHARS:
            text = text[:_OUTREACH_EXCERPT_CHARS].rstrip() + "…"
        if text:
            lines.append(f"- 約 {elapsed_min} 分鐘前，你主動傳了：「{text}」")
        else:
            lines.append(f"- 約 {elapsed_min} 分鐘前，你主動傳了訊息給對方")
    if not lines:
        return ""
    return "\n\n你最近主動聯絡對方的紀錄：\n" + "\n".join(lines)


def _persona_block(
    character: Character,
    *,
    relationship_context_lines: tuple[str, ...] = (),
    interaction_context_lines: tuple[str, ...] = (),
) -> list[str]:
    lines = [f"- 名稱：{character.name}"]
    lines.extend(render_character_identity_lines(character))
    if character.summary:
        lines.append(f"- 簡介：{character.summary[:160]}")
    if character.personality:
        lines.append("- 性格：" + "、".join(character.personality[:6]))
    lines.extend(character.personality_type.to_prompt_lines())
    if character.speaking_style:
        lines.append(f"- 說話風格：{character.speaking_style[:120]}")
    state = character.state
    lines.extend((
        f"- 當前情緒：{state.emotion}",
        f"- 好感狀態：{_affection_tone(state.affection)}",
        f"- 信任狀態：{_trust_tone(state.trust)}",
        f"- 精力狀態：{_energy_tone(state.energy)}",
        f"- 疲勞狀態：{_fatigue_tone(state.fatigue)}",
    ))
    for line in relationship_context_lines:
        text = line.strip()
        if text:
            lines.append(text)
    for line in interaction_context_lines:
        text = line.strip()
        if text:
            lines.append(text)
    return lines


def _activity_block(
    activity: ScheduleActivity | None,
    *,
    now: datetime,
    local_tz: tzinfo | None,
) -> list[str]:
    if activity is None:
        return [
            "目前活動：使用者傳訊息時，角色沒有明確的排程活動進行中"
            "（但仍可能很累 / 心思不在這裡）。",
        ]
    start_local = _to_local(activity.start_at, local_tz)
    end_local = _to_local(activity.end_at, local_tz)
    now_local = _to_local(now, local_tz)
    duration_remain = (activity.end_at - now).total_seconds() / 60.0
    desc = activity.description.strip() or activity.category
    loc = f"（地點：{activity.location}）" if activity.location else ""
    return [
        "目前活動：",
        f"- 時段：{start_local:%H:%M}–{end_local:%H:%M}"
        f"（現在 {now_local:%H:%M}，剩餘約 {max(0, int(duration_remain))} 分鐘）",
        f"- 類別：{activity.category}",
        f"- 內容：{desc}{loc}",
        f"- busy_score：{activity.busy_score:.2f}（0=隨時可回，1=幾乎不能碰手機）",
    ]


def _to_local(value: datetime, local_tz: tzinfo | None) -> datetime:
    aware = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if local_tz is not None:
        return aware.astimezone(local_tz)
    return aware.astimezone(timezone.utc)


# ----------------------------------------------------------------------
# Output parsing
# ----------------------------------------------------------------------


_MODE_RE = re.compile(r"模式\s*[:：]\s*(.*)")
_BRIEF_RE = re.compile(r"短回覆\s*[:：]\s*(.*)")
_UNTIL_RE = re.compile(r"延後到\s*[:：]\s*(.*)")
_REASON_RE = re.compile(r"原因\s*[:：]\s*(.*)")
_FENCE_RE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")
_HHMM_RE = re.compile(r"^(\d{1,2})[:：](\d{2})$")


def _parse(
    raw: str,
    *,
    now: datetime,
    current_activity: ScheduleActivity | None,
    local_tz: tzinfo | None,
) -> BusyDecision:
    text = (raw or "").strip()
    if not text:
        return BusyDecision()
    text = _FENCE_RE.sub("", text).strip()
    mode_text = ""
    brief_text = ""
    until_text = ""
    reason_text = ""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        m = _MODE_RE.match(stripped)
        if m:
            mode_text = m.group(1).strip()
            continue
        m = _BRIEF_RE.match(stripped)
        if m:
            brief_text = m.group(1).strip()
            continue
        m = _UNTIL_RE.match(stripped)
        if m:
            until_text = m.group(1).strip()
            continue
        m = _REASON_RE.match(stripped)
        if m:
            reason_text = m.group(1).strip()

    if not _looks_like_defer(mode_text):
        return BusyDecision()

    brief = _clean_brief(brief_text)
    if not brief:
        # Model said "defer" but didn't write the ack — without the ack
        # the user gets a silent void, which is strictly worse than just
        # answering immediately. Fall back.
        return BusyDecision()

    defer_until = _parse_defer_until(
        until_text,
        now=now,
        current_activity=current_activity,
        local_tz=local_tz,
    )
    reason = _clean_text(reason_text, _MAX_REASON_CHARS)
    return BusyDecision(
        mode=BusyReplyMode.BRIEF_DEFER,
        brief_reply=brief,
        defer_until=defer_until,
        defer_reason=reason,
    )


def _looks_like_defer(text: str) -> bool:
    cleaned = text.strip().strip("「」\"'").lower()
    if not cleaned:
        return False
    if "延後" in cleaned or "稍後" in cleaned or "晚點" in cleaned:
        return True
    return cleaned in {"defer", "brief_defer", "later"}


def _clean_brief(text: str) -> str:
    cleaned = text.strip().strip("「」\"'")
    if not cleaned:
        return ""
    if len(cleaned) > _MAX_BRIEF_REPLY_CHARS:
        # Likely model wrote a full reply instead of an ack. Drop.
        return ""
    return cleaned


def _clean_text(text: str, limit: int) -> str:
    cleaned = text.strip().strip("「」\"'")
    if not cleaned:
        return ""
    if len(cleaned) > limit:
        cleaned = cleaned[:limit].rstrip() + "…"
    return cleaned


def _parse_defer_until(
    raw: str,
    *,
    now: datetime,
    current_activity: ScheduleActivity | None,
    local_tz: tzinfo | None,
) -> datetime | None:
    candidate = raw.strip().strip("「」\"'")
    if not candidate:
        return _default_defer_until(now, current_activity)
    # ISO datetime
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        parsed = None
    if parsed is not None:
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=local_tz or timezone.utc)
        return _clamp_min_defer(parsed.astimezone(timezone.utc), now)
    # HH:MM today (character local)
    match = _HHMM_RE.match(candidate)
    if match:
        hour = int(match.group(1))
        minute = int(match.group(2))
        if 0 <= hour < 24 and 0 <= minute < 60:
            tz = local_tz or timezone.utc
            now_local = now.astimezone(tz)
            target_local = datetime.combine(
                now_local.date(), time(hour=hour, minute=minute), tz,
            )
            if target_local <= now_local:
                # The model probably meant "tomorrow" or "later today
                # rolled over". Bump by one day for a reasonable defer
                # rather than firing immediately.
                target_local = target_local + timedelta(days=1)
            return _clamp_min_defer(
                target_local.astimezone(timezone.utc), now,
            )
    return _default_defer_until(now, current_activity)


def _default_defer_until(
    now: datetime,
    current_activity: ScheduleActivity | None,
) -> datetime:
    if current_activity is not None and current_activity.end_at > now:
        return current_activity.end_at
    # No activity / activity already ended — give a 30-minute soft floor
    # so the dispatcher has *some* breathing room before retrying.
    return now + timedelta(minutes=30)


_MIN_DEFER_LEAD = timedelta(minutes=1)
"""``scheduled_for`` must sit at least this far in the future, otherwise
the very next tick would release the row before the user even sees the
brief ack render — feels like a glitch."""


def _clamp_min_defer(value: datetime, now: datetime) -> datetime:
    if value < now + _MIN_DEFER_LEAD:
        return now + _MIN_DEFER_LEAD
    return value
