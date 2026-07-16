"""LLM-backed proactive intention judge.

The cheap gate answers "is a proactive push allowed?". This judge answers
"does the character have a meaningful reason to spend a proactive slot
right now?". It deliberately asks for inner motive, conversation purpose,
and expected reply before the message composer gets a chance to write.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone, tzinfo

from kokoro_link.application.services.model_resolver import ModelResolver
from kokoro_link.infrastructure.llm.cloud_refusal import (
    log_auxiliary_llm_failure,
)
from kokoro_link.contracts.active_llm import ActiveLLMProviderPort
from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.contracts.proactive import ProactiveContext
from kokoro_link.contracts.proactive_intention import (
    ProactiveIntentionDecision,
    ProactiveIntentionJudgePort,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.schedule import ScheduleActivity
from kokoro_link.domain.value_objects.proactive_trigger import ProactiveTrigger
from kokoro_link.domain.value_objects.timezone import to_timezone
from kokoro_link.infrastructure.prompt.character_identity import (
    render_character_identity_lines,
)
from kokoro_link.infrastructure.prompt.operator_language import (
    render_operator_language_hint,
)
from kokoro_link.infrastructure.prompt.persona_curiosity import (
    render_persona_curiosity_plan_lines,
)
from kokoro_link.infrastructure.prompt.proactive_streak import (
    render_unanswered_streak_lines,
)
from kokoro_link.infrastructure.prompt.role_boundary import (
    render_role_knowledge_boundary_lines,
)
from kokoro_link.infrastructure.prompt.timing_utils import (
    describe_idle_natural,
    format_local_current_time,
    render_subjective_time_topical_hint,
)
from kokoro_link.infrastructure.prompts import get_default_loader

_LOGGER = logging.getLogger(__name__)
_MAX_REASON_CHARS = 160


class NullProactiveIntentionJudge(ProactiveIntentionJudgePort):
    """Pass-through judge used when the feature is intentionally disabled."""

    async def judge(
        self, context: ProactiveContext,
    ) -> ProactiveIntentionDecision:
        return ProactiveIntentionDecision(
            should_consume_slot=True,
            reason="intention judge disabled",
        )


class LLMProactiveIntentionJudge(ProactiveIntentionJudgePort):
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
        self, context: ProactiveContext,
    ) -> ProactiveIntentionDecision:
        if _is_promise_fulfilment(context.trigger):
            return ProactiveIntentionDecision(
                should_consume_slot=True,
                reason=f"trigger={context.trigger.value} promise fulfilment",
            )
        if await self._resolver.is_fake(character=context.character):
            return ProactiveIntentionDecision(
                should_consume_slot=False,
                reason="fake provider cannot judge proactive intention",
            )

        prompt = _build_prompt(context)
        try:
            raw = await self._resolver.generate(
                prompt, character=context.character,
            )
        except Exception as exc:
            log_auxiliary_llm_failure(
                _LOGGER, exc,
                "proactive intention judge LLM call failed character=%s",
                context.character.id,
            )
            return ProactiveIntentionDecision(
                should_consume_slot=False,
                reason="intention judge LLM call failed",
            )

        payload = _extract_json_object(raw)
        if payload is None:
            return ProactiveIntentionDecision(
                should_consume_slot=False,
                reason="intention judge output contained no JSON object",
            )
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError as exc:
            return ProactiveIntentionDecision(
                should_consume_slot=False,
                reason=f"intention judge JSON unparseable: {exc.msg}",
            )
        if not isinstance(parsed, dict):
            return ProactiveIntentionDecision(
                should_consume_slot=False,
                reason="intention judge JSON was not an object",
            )

        reason = _clamp(_coerce_str(parsed.get("reason")), _MAX_REASON_CHARS)
        return ProactiveIntentionDecision(
            should_consume_slot=bool(parsed.get("should_consume_slot", False)),
            reason=reason or "(intention judge gave no reason)",
            inner_motive=_clamp(_coerce_str(parsed.get("inner_motive")), 240),
            conversation_purpose=_clamp(
                _coerce_str(parsed.get("conversation_purpose")), 180,
            ),
            expected_reply=_clamp(_coerce_str(parsed.get("expected_reply")), 180),
            risk=_clamp(_coerce_str(parsed.get("risk")), 180),
            best_timing=_clamp(_coerce_str(parsed.get("best_timing")), 80),
        )


def _build_prompt(context: ProactiveContext) -> str:
    character = context.character
    return get_default_loader().render(
        "proactive/intention_judge",
        # ``reason`` is explicitly "一句話給操作者看的判斷理由" and renders
        # in ChannelProactiveAttemptLog.vue, so it must follow the
        # operator's content language (bug B2 class).
        language_hint=render_operator_language_hint(
            context.operator_primary_language,
        ),
        persona_block="\n".join(_persona_lines(character)),
        role_boundary_block="\n".join(render_role_knowledge_boundary_lines()),
        interaction_block="\n".join(_interaction_lines(context)),
        now_local=format_local_current_time(context.now, context.local_tz),
        schedule_block="\n".join(_schedule_lines(context)),
        optional_recent_sent=_optional_recent_sent_block(context),
        optional_unanswered_streak=_optional_unanswered_streak_block(context),
        optional_dialogue_summary=_optional_dialogue_summary_block(context),
        optional_initial_relationship=_optional_initial_relationship_block(context),
        optional_operator_persona=_optional_operator_persona_block(context),
        optional_persona_curiosity=_optional_persona_curiosity_block(context),
        optional_memories=_optional_memories_block(context),
        optional_active_goals=_optional_active_goals_block(context),
        optional_calendar=_optional_calendar_block(context),
        optional_weather=_optional_weather_block(context),
        optional_world_event=_optional_world_event_block(context),
        optional_story_events=_optional_story_events_block(context),
        optional_active_arc=_optional_active_arc_block(context),
        optional_deferred_intents=_optional_deferred_intents_block(context),
        optional_pace_preference=_optional_pace_preference_block(context),
        optional_subjective_time=_optional_subjective_time_block(context),
    )


def _section(header: str, body_lines: list[str]) -> str:
    """Prefix a section header + body with a blank-line separator.

    Returns an empty string when ``body_lines`` is empty so optional
    template slots collapse cleanly.
    """
    if not body_lines:
        return ""
    return "\n\n" + header + "\n" + "\n".join(body_lines)


def _optional_recent_sent_block(context: ProactiveContext) -> str:
    if not context.recent_sent_attempts:
        return ""
    return _section(
        "最近已送出的主動訊息（新到舊；不要為同一題材重複消耗額度）：",
        _recent_sent_lines(context),
    )


def _optional_unanswered_streak_block(context: ProactiveContext) -> str:
    """Surface the consecutive-unanswered streak so the judge can tell a
    cheap "刷存在感的重複" apart from an authentic, evolving reaction to
    being ignored. Shared phrasing with the decider keeps the two paths
    from pulling in opposite directions on the same streak."""
    lines = render_unanswered_streak_lines(context.unanswered_streak)
    if not lines:
        return ""
    return "\n\n" + "\n".join(lines)


def _optional_dialogue_summary_block(context: ProactiveContext) -> str:
    summary = context.recent_dialogue_summary.strip()
    if not summary:
        return ""
    return _section("最近對話摘要：", [summary[:700]])


def _optional_operator_persona_block(context: ProactiveContext) -> str:
    cleaned = [line for line in context.operator_persona_lines if line.strip()]
    if not cleaned:
        return ""
    return _section(
        "你對使用者逐步認識到的事（只能自然使用，不可拿私密資料硬開場）：",
        cleaned[:8],
    )


def _optional_initial_relationship_block(context: ProactiveContext) -> str:
    cleaned = [line for line in context.initial_relationship_lines if line.strip()]
    if not cleaned:
        return ""
    return _section(
        "使用者創角時確認的起始關係設定（調整稱謂、距離與主動訊息邊界；不可當成已發生過的系統內記憶）：",
        [
            *cleaned[:12],
            "- 若這是首則或早期主動訊息，不可提不存在的共同回憶，不可假設對方當下狀態。",
        ],
    )


def _optional_persona_curiosity_block(context: ProactiveContext) -> str:
    lines = render_persona_curiosity_plan_lines(
        context.persona_curiosity_plan,
        surface="proactive",
    )
    if not lines:
        return ""
    return _section("自然認識對方的候選意圖（不是必發理由）：", lines)


def _optional_memories_block(context: ProactiveContext) -> str:
    memories = context.recent_memories_text.strip()
    if not memories:
        return ""
    return _section("最近記憶片段：", [memories[:900]])


def _optional_active_goals_block(context: ProactiveContext) -> str:
    goals = context.active_goals_text.strip()
    if not goals:
        return ""
    return _section("角色目前在意的目標：", [goals[:600]])


def _optional_calendar_block(context: ProactiveContext) -> str:
    calendar = context.calendar_context.strip()
    if not calendar:
        return ""
    return _section("真實世界行事曆：", [calendar[:600]])


def _optional_weather_block(context: ProactiveContext) -> str:
    weather = context.weather_context.strip()
    if not weather:
        return ""
    # Weather block ships with its own header inline, so emit a bare
    # blank-line separator without a synthetic title.
    return "\n\n" + weather[:600]


def _optional_world_event_block(context: ProactiveContext) -> str:
    title = context.world_event_seed_title
    if not title:
        return ""
    body = [
        f"- 標題：{title[:180]}",
        "- 這個素材可能是角色自己在意，也可能只是和對方的公開背景有關；"
        "請判斷角色能否用符合自身身份、年齡、知識深度與說話風格的方式自然提起，"
        "不要假裝專家。",
    ]
    if context.world_event_seed_source:
        body.append(f"- 來源：{context.world_event_seed_source[:120]}")
    if context.world_event_seed_locale:
        body.append(f"- 來源地區：{context.world_event_seed_locale[:40]}")
    if context.operator_location_context:
        body.append(f"- {context.operator_location_context[:160]}")
    if context.world_event_seed_summary:
        body.append(f"- 摘要：{context.world_event_seed_summary[:500]}")
    return _section(
        "外界消息候選素材（不是角色親身經歷；是否使用仍需判斷）：",
        body,
    )


def _optional_story_events_block(context: ProactiveContext) -> str:
    if not context.story_events:
        return ""
    body = [
        f"- {event.narrative.strip()[:220]}"
        for event in context.story_events[:4]
        if event.narrative.strip()
    ]
    if not body:
        return ""
    return _section("今日角色身上發生的小事（素材，不等於必須推播）：", body)


def _optional_active_arc_block(context: ProactiveContext) -> str:
    arc = context.active_arc
    if arc is None:
        return ""
    return _section(
        "目前故事線：",
        [f"- {arc.title}：{arc.premise[:260]}"],
    )


_PACE_PHRASES: dict[str, str] = {
    "more_active": (
        "對方明確希望這個角色「主動一點 / 多話一點」——"
        "在通過其他標準的前提下，可以稍微放寬主動傳訊息的傾向；"
        "但仍要符合角色性格與當下時機，不要因此變成廣告或刷存在感。"
    ),
    "balanced": (
        "對方對對話節奏沒有特別偏好；維持角色既有的內在動機節奏即可。"
    ),
    "more_quiet": (
        "對方明確希望這個角色「安靜一點 / 多留白」——"
        "在通過其他標準的前提下，更傾向保留額度；只在動機特別清楚時消耗 slot。"
    ),
}


def _optional_subjective_time_block(context: ProactiveContext) -> str:
    """HUMANIZATION_ROADMAP §4.4 — topical-layer 久未聯絡 catch-up hint.

    Sibling to ``idle_drift`` EmotionEvent (emotional layer); this block
    informs *topic selection* (catch-up first, don't yank prior thread).
    Returns empty when the idle gap is short or unknown so the prompt
    stays minimal in the steady-state, on-going-conversation case.
    """
    lines = render_subjective_time_topical_hint(context.idle_minutes)
    if not lines:
        return ""
    return "\n\n" + "\n".join(lines)


def _optional_pace_preference_block(context: ProactiveContext) -> str:
    """HUMANIZATION_ROADMAP §3.6 + §4.2 — operator register / pace section.

    Owner decision (2026-05-21): the **observed** address preference
    (§4.2 ``OperatorAddressPreference``) takes priority over the
    user-explicit pace preference (§3.6). When both exist we surface
    the observed value and keep the pace preference as a secondary
    bullet so the LLM still sees both signals. When only pace exists
    we fall back to the §3.6 standalone phrasing.

    LLM-first 紅線: still a *bias* the LLM weighs — never collapsed
    into an if-else branch downstream.
    """
    observed_lines = _render_address_preference_lines(
        context.address_preference,
        resolved_salutation=context.resolved_character_salutation,
    )
    pace_phrase = _PACE_PHRASES.get(
        (context.character.operator_pace_preference or "").strip(),
    )
    if not observed_lines and not pace_phrase:
        return ""
    bullets: list[str] = []
    bullets.extend(observed_lines)
    if pace_phrase:
        # Demote the explicit pace knob to a "secondary" cue when the
        # observation already exists — the LLM still sees both, just
        # ordered so the freshest signal leads.
        prefix = "- " if not observed_lines else "- 〔顯式設定〕"
        bullets.append(f"{prefix}{pace_phrase}")
    return _section("對方對這個角色的期望節奏：", bullets)


def _render_address_preference_lines(
    pref: "OperatorAddressPreference | None",
    *,
    resolved_salutation: str | None = None,
) -> list[str]:
    # The resolved character-direction salutation (seed > observed) owns
    # the 「對方稱呼這個角色」 slot when it carries a real signal, so an
    # explicit per-character seed name surfaces even before any
    # observation. Falls back to the raw observed salutation otherwise.
    salutation = (resolved_salutation or "").strip()
    if pref is None or pref.is_empty:
        if salutation:
            return [f"- 對方稱呼這個角色：{salutation}"]
        return []
    lines: list[str] = []
    salutation = salutation or pref.salutation
    if salutation:
        lines.append(f"- 對方稱呼這個角色：{salutation}")
    formality_phrase = _FORMALITY_PHRASES.get(pref.formality_level)
    if formality_phrase:
        lines.append(f"- 對方說話的敬語層級：{formality_phrase}")
    length_phrase = _LENGTH_PHRASES.get(pref.response_length_pref)
    if length_phrase:
        lines.append(f"- 對方似乎偏好的回覆長度：{length_phrase}")
    return lines


_FORMALITY_PHRASES: dict[str, str] = {
    "low": "很放鬆 / 不太用敬語（暱稱、口語、表情符號常見）",
    "medium": "中等（一般對話禮貌但不過度正式）",
    "high": "明顯偏正式（使用敬語、不省略主詞、語句完整）",
}

_LENGTH_PHRASES: dict[str, str] = {
    "short": "偏短句、快節奏（一兩句就丟下一個話題）",
    "medium": "中等長度（句子完整但不冗長）",
    "long": "偏長段、願意慢慢說明（願意讀完一段話）",
}


def _optional_deferred_intents_block(context: ProactiveContext) -> str:
    """HUMANIZATION_ROADMAP §3.4 — re-surface motives that prior judge
    calls blocked but kept under TTL.

    The block is a *fact* layer: it states what the character previously
    wanted to say, why it was held back, and when the LLM itself
    suggested would be a better timing. The decision whether to act on
    them this round belongs to the LLM. We do not pre-rank, do not
    auto-promote, do not collapse to a score.
    """
    if not context.deferred_intents:
        return ""
    now = context.now
    body: list[str] = []
    for intent in context.deferred_intents[:5]:
        elapsed_minutes = max(
            0.0,
            (now - intent.created_at).total_seconds() / 60.0,
        )
        remaining_minutes = max(
            0.0,
            (intent.expires_at - now).total_seconds() / 60.0,
        )
        parts: list[str] = [
            f"- 想做的事：{intent.inner_motive[:200]}",
        ]
        if intent.conversation_purpose:
            parts.append(f"  · 對話目的：{intent.conversation_purpose[:160]}")
        if intent.expected_reply:
            parts.append(f"  · 期待對方的回應：{intent.expected_reply[:160]}")
        if intent.risk:
            parts.append(f"  · 上次判斷的風險：{intent.risk[:160]}")
        if intent.best_timing:
            parts.append(f"  · 上次建議時機：{intent.best_timing[:80]}")
        if intent.reason:
            parts.append(f"  · 上次未發的原因：{intent.reason[:160]}")
        parts.append(
            f"  · 已等候 {_format_elapsed_minutes(elapsed_minutes)}，"
            f"距離自然遺忘還有約 {_format_elapsed_minutes(remaining_minutes)}",
        )
        body.append("\n".join(parts))
    return _section(
        "先前你曾想過、但被自己壓下來的念頭（請判斷時機是否到了，或讓它自然淡掉）：",
        body,
    )


def _persona_lines(character: Character) -> list[str]:
    lines = [f"- 名稱：{character.name}"]
    lines.extend(render_character_identity_lines(character))
    if character.summary:
        lines.append(f"- 背景：{character.summary[:220]}")
    if character.personality:
        lines.append("- 性格：" + "、".join(character.personality[:8]))
    if character.speaking_style:
        lines.append(f"- 說話風格：{character.speaking_style[:180]}")
    if character.interests:
        lines.append("- 興趣：" + "、".join(character.interests[:8]))
    lines.extend(character.disposition.to_prompt_lines())
    lines.extend(character.personality_type.to_prompt_lines())
    state = character.state
    lines.append(
        f"- 當前狀態：情緒 {state.emotion}，精力 {state.energy}/100，"
        f"疲勞 {state.fatigue}/100，信任 {state.trust}/100",
    )
    return lines


def _interaction_lines(context: ProactiveContext) -> list[str]:
    lines: list[str] = []
    if context.idle_minutes is None:
        lines.append("- 你和對方還沒有對話紀錄")
    else:
        lines.append(
            f"- 對方上次發話：{describe_idle_natural(context.idle_minutes)}",
        )
    lines.append(
        f"- 今天已送出主動訊息 {context.sent_today} 次"
        f"（上限 {context.character.proactive_daily_limit}）",
    )
    remaining = max(0, context.character.proactive_daily_limit - context.sent_today)
    lines.append(f"- 今日剩餘額度：{remaining}")
    if context.last_proactive_at is not None:
        elapsed = (context.now - context.last_proactive_at).total_seconds() / 60.0
        lines.append(f"- 上次通過主動評估約 {elapsed:.0f} 分鐘前")
    lines.append(f"- 觸發來源：{context.trigger.value}")
    return lines


def _schedule_lines(context: ProactiveContext) -> list[str]:
    lines: list[str] = []
    if context.current_activity is not None:
        lines.append(
            f"- {_describe_activity(context.current_activity, prefix='正在', local_tz=context.local_tz)}"
        )
    else:
        lines.append("- 目前沒有正在進行的排程活動")
        if context.just_finished_activity is not None:
            lines.append(
                f"- {_describe_activity(context.just_finished_activity, prefix='剛結束', local_tz=context.local_tz)}",
            )
    if context.upcoming_activities:
        snippets = [
            _describe_activity(activity, prefix="", local_tz=context.local_tz)
            for activity in context.upcoming_activities[:3]
        ]
        lines.append("- 接下來：" + "；".join(snippets))
    if context.upcoming_day_schedules:
        for schedule in context.upcoming_day_schedules[:2]:
            snippets = [
                f"{to_timezone(act.start_at, context.local_tz).strftime('%H:%M')} {act.description}"
                for act in schedule.activities[:3]
            ]
            if snippets:
                lines.append(f"- {schedule.date.isoformat()}：" + "；".join(snippets))
    return lines


def _recent_sent_lines(context: ProactiveContext) -> list[str]:
    lines: list[str] = []
    idle_minutes = context.idle_minutes
    for attempt in context.recent_sent_attempts[:3]:
        elapsed = (context.now - attempt.decided_at).total_seconds() / 60.0
        if idle_minutes is None:
            reply_tag = ""
        elif idle_minutes < elapsed:
            reply_tag = "（對方已回）"
        else:
            reply_tag = "（對方還沒回）"
        lines.append(
            f"- {_format_elapsed_minutes(elapsed)}{reply_tag}："
            f"{(attempt.message or '').strip()[:240] or '(無內容)'}",
        )
    return lines


def _describe_activity(
    activity: ScheduleActivity,
    *,
    prefix: str,
    local_tz: tzinfo,
) -> str:
    start = to_timezone(activity.start_at, local_tz).strftime("%H:%M")
    end = to_timezone(activity.end_at, local_tz).strftime("%H:%M")
    head = f"{prefix}：" if prefix else ""
    desc = activity.description.strip() or activity.category
    loc = f" @ {activity.location}" if activity.location else ""
    return f"{head}{start}-{end} {desc}（{activity.category}，busy={activity.busy_score:.2f}{loc}）"


def _format_elapsed_minutes(minutes: float) -> str:
    if minutes < 60:
        return f"{int(round(minutes))} 分鐘前"
    hours = minutes / 60.0
    if hours < 24:
        return f"{hours:.1f} 小時前"
    return f"{hours / 24.0:.1f} 天前"


def _is_promise_fulfilment(trigger: ProactiveTrigger) -> bool:
    return trigger in (
        ProactiveTrigger.PENDING_FOLLOW_UP,
        ProactiveTrigger.SCHEDULED_PROMISE,
    )


def _coerce_str(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _clamp(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[:limit].rstrip() + "..."


def _extract_json_object(text: str) -> str | None:
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


_ = (datetime, timezone)
