"""LLM-backed proactive decider.

Takes the full ``ProactiveContext`` the dispatcher built, formats it
into a first-person Chinese prompt, and asks a ``ChatModelPort`` to
decide whether the character should say anything right now. The prompt
is deliberately biased toward silence — LLMs tend to please by default,
and an over-talkative proactive system burns trust fast.

JSON parsing is tolerant (code fences / preambles allowed). Any
failure — LLM timeout, unparseable output, missing fields — becomes a
"don't send" decision with a descriptive ``reason`` so the operator
can see it in the audit log.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, tzinfo

from kokoro_link.application.services.model_resolver import ModelResolver
from kokoro_link.contracts.active_llm import ActiveLLMProviderPort
from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.contracts.proactive import (
    ProactiveContext,
    ProactiveDecision,
    ProactiveDeciderPort,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.schedule import ScheduleActivity
from kokoro_link.domain.value_objects.tool_call import ToolCall
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
    render_current_time_fact_lines,
    render_subjective_time_topical_hint,
)
from kokoro_link.infrastructure.prompts import get_default_loader

_LOGGER = logging.getLogger(__name__)
_MAX_MESSAGE_CHARS = 300
_MAX_TOOL_CALLS_PER_DECISION = 1


class LLMProactiveDecider(ProactiveDeciderPort):
    def __init__(
        self,
        *,
        model: ChatModelPort | None = None,
        provider: ActiveLLMProviderPort | None = None,
        max_message_chars: int = _MAX_MESSAGE_CHARS,
    ) -> None:
        self._resolver = ModelResolver(provider=provider, model=model)
        self._max_message_chars = max_message_chars

    async def decide(self, context: ProactiveContext) -> ProactiveDecision:
        if await self._resolver.is_fake(character=context.character):
            return ProactiveDecision(
                should_send=False,
                reason="fake provider selected",
                message=None,
            )
        prompt = _build_prompt(context)
        try:
            raw = await self._resolver.generate(prompt, character=context.character)
        except Exception as exc:
            _LOGGER.exception("proactive LLM call failed")
            return ProactiveDecision(
                should_send=False,
                reason=f"LLM call raised: {type(exc).__name__}",
                message=None,
            )

        payload = _extract_json_object(raw)
        if payload is None:
            return ProactiveDecision(
                should_send=False,
                reason="LLM output contained no JSON object",
                message=None,
            )
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError as exc:
            return ProactiveDecision(
                should_send=False,
                reason=f"LLM JSON unparseable: {exc.msg}",
                message=None,
            )
        if not isinstance(parsed, dict):
            return ProactiveDecision(
                should_send=False,
                reason="LLM JSON was not an object",
                message=None,
            )

        should_send = bool(parsed.get("should_send", False))
        reason = _coerce_str(parsed.get("reason")) or "(LLM gave no reason)"
        raw_message = parsed.get("message")
        message = _coerce_str(raw_message)
        tool_calls = _parse_tool_calls(
            parsed.get("tool_calls"),
            allowed={t.name for t in context.available_tools},
        )

        if not should_send:
            return ProactiveDecision(
                should_send=False, reason=reason, message=None,
            )
        if not message:
            return ProactiveDecision(
                should_send=False,
                reason="LLM said should_send=true but gave no message",
                message=None,
            )
        if len(message) > self._max_message_chars:
            message = message[: self._max_message_chars].rstrip() + "…"
        return ProactiveDecision(
            should_send=True, reason=reason, message=message,
            tool_calls=tool_calls,
        )


def _build_prompt(context: ProactiveContext) -> str:
    character = context.character
    state = character.state

    sections: list[str] = []

    language_hint = render_operator_language_hint(context.operator_primary_language)
    if language_hint:
        sections.append(language_hint)

    sections.append(
        f"你是角色「{character.name}」，正在考慮是否主動向使用者傳訊息。"
    )

    identity = [
        *render_character_identity_lines(character),
        f"- 性格：{_join_list(character.personality) or '(未設定)'}",
        f"- 說話風格：{character.speaking_style or '(未設定)'}",
    ]
    if character.summary:
        identity.append(f"- 背景：{character.summary}")
    if character.interests:
        identity.append(f"- 興趣：{_join_list(character.interests)}")
    if character.boundaries:
        identity.append(f"- 禁忌：{_join_list(character.boundaries)}")
    # 內在動機傾向 —— 全 medium 時 to_prompt_lines 回空 list，自動跳過。
    # 在 proactive prompt 裡這四維特別關鍵：sharing_drive 直接影響 LLM
    # 該不該主動開口的判斷傾向（但**仍然走 LLM 判斷、不做硬閘**）。
    identity.extend(character.disposition.to_prompt_lines())
    identity.extend(character.personality_type.to_prompt_lines())
    sections.append("角色設定：\n" + "\n".join(identity))

    sections.append("\n".join(render_role_knowledge_boundary_lines()))

    if context.initial_relationship_lines:
        relationship_lines = [
            line for line in context.initial_relationship_lines if line.strip()
        ]
        if relationship_lines:
            sections.append(
                "\n".join(relationship_lines)
                + "\n- 首則或早期主動訊息只能引用這些明示設定作為語氣與邊界來源；"
                "不可說成你們已經在系統內聊過或共同經歷過。"
            )

    if context.operator_persona_lines:
        persona_lines = [
            line for line in context.operator_persona_lines if line.strip()
        ]
        if persona_lines:
            sections.append(
                "你對使用者逐步認識到的事（只當背景，不要每次都主動提起；"
                "若與當下話題無關就不要硬塞）：\n"
                + "\n".join(persona_lines)
                + "\n- 使用時要克制自然；不要背誦資料，不要用私密資訊當開場。"
            )

    persona_curiosity_lines = render_persona_curiosity_plan_lines(
        context.persona_curiosity_plan,
        surface="proactive",
    )
    if persona_curiosity_lines:
        sections.append("\n".join(persona_curiosity_lines))

    state_lines = [
        f"- 情緒：{state.emotion}",
        f"- 好感：{state.affection}/100",
        f"- 疲勞：{state.fatigue}/100",
        f"- 精力：{state.energy}/100",
        f"- 信任：{state.trust}/100",
    ]
    if state.current_intent:
        state_lines.append(f"- 當下意圖：{state.current_intent}")
    sections.append("當前狀態：\n" + "\n".join(state_lines))

    time_lines = render_current_time_fact_lines(
        context.now, context.local_tz, heading=None,
    )
    if context.current_activity is not None:
        time_lines.append(
            f"- {_describe_activity(context.current_activity, prefix='你正在', local_tz=context.local_tz)}"
        )
    else:
        time_lines.append("- 你目前是轉場空檔，沒有正在進行的活動")
        if context.just_finished_activity is not None:
            time_lines.append(
                f"- {_describe_activity(context.just_finished_activity, prefix='剛結束', local_tz=context.local_tz)}"
            )
    if context.upcoming_activities:
        upcoming_strs = [
            _describe_activity(a, prefix="", local_tz=context.local_tz)
            for a in context.upcoming_activities[:3]
        ]
        time_lines.append("- 接下來：" + "；".join(upcoming_strs))
    sections.append(
        "行程（此為你此刻身處地點與正在做的事的**唯一真實來源**；"
        "其他段落如故事、劇情線只是話題素材，若與此段衝突一律以此段為準）：\n"
        + "\n".join(time_lines)
    )

    interaction_lines: list[str] = []
    if context.idle_minutes is None:
        interaction_lines.append("- 你和使用者還沒有過對話")
    else:
        interaction_lines.append(
            f"- 使用者上次發話：{describe_idle_natural(context.idle_minutes)}"
        )
    interaction_lines.append(
        f"- 你今天已主動開口 {context.sent_today} 次"
        f"（本日上限 {character.proactive_daily_limit}）"
    )
    if context.last_proactive_at is not None:
        elapsed = (context.now - context.last_proactive_at).total_seconds() / 60.0
        interaction_lines.append(
            f"- 你上次（不論是否真的送出）做主動評估是 {elapsed:.0f} 分鐘前"
        )
    interaction_lines.append(f"- 這次評估的觸發原因：{context.trigger.value}")
    sections.append("互動近況：\n" + "\n".join(interaction_lines))

    # HUMANIZATION_ROADMAP §4.4: when the idle gap is large enough, expose
    # the topical-layer "久未聯絡 catch-up" hint as its own section so the
    # decider can shape opening choice without conflating it with the
    # idle-drift emotional signal.
    topical_hint = render_subjective_time_topical_hint(context.idle_minutes)
    if topical_hint:
        sections.append("\n".join(topical_hint))

    # Self-history: showing the decider exactly what it said recently
    # is the single biggest lever against the "same topic re-asked every
    # cooldown" failure mode. We also flag which ones the user never
    # answered so the prompt can tell it to back off instead of
    # rephrasing the same question.
    if context.recent_sent_attempts:
        idle_minutes = context.idle_minutes
        history_lines = [
            "你最近幾次主動傳出去的訊息（新→舊；這些話已經送出去了，"
            "不要再用同樣的題材、同樣的問題重問一次）：",
        ]
        for att in context.recent_sent_attempts[:5]:
            elapsed_min = (context.now - att.decided_at).total_seconds() / 60.0
            when_text = _format_elapsed_minutes(elapsed_min)
            # User has replied iff their latest message came AFTER this
            # proactive. idle_minutes == minutes since user's last turn;
            # if that's smaller than elapsed_min the user spoke after
            # the proactive → they replied.
            if idle_minutes is None:
                reply_tag = ""
            elif idle_minutes < elapsed_min:
                reply_tag = "（對方已回）"
            else:
                reply_tag = "（對方還沒回）"
            text = (att.message or "").strip() or "(無內容)"
            history_lines.append(f"- {when_text}{reply_tag}：{text}")
        history_lines.append(
            "這些都已經送出去了。若要再開口，必須是真正不同的方向／角度／心境，"
            "不能只是把上面的話換句型重講。"
        )
        sections.append("\n".join(history_lines))

    # Consecutive-unanswered streak: the fact that lets the character
    # *evolve* (interest → worry → sulking → giving space) across days
    # of being ignored instead of re-deriving the same opener. Shared
    # with the intention judge so both paths react to the same number.
    streak_lines = render_unanswered_streak_lines(context.unanswered_streak)
    if streak_lines:
        sections.append("\n".join(streak_lines))

    if context.calendar_context.strip():
        sections.append(
            "今天的真實世界行事曆（事實層；自行依角色身分與性格判斷今天該怎麼過，"
            "不要假設大家作息都一樣）：\n"
            + context.calendar_context.strip()
        )

    # 天氣事實層 —— 跟 chat / planner / feed 共用同一筆事實，避免主動
    # 訊息聲稱「外面好天氣」但 feed 同時貼出去下雨的場景。
    if context.weather_context.strip():
        sections.append(context.weather_context.strip())

    upcoming_block = _render_upcoming_days_for_decider(context)
    if upcoming_block:
        sections.append(upcoming_block)

    if context.recent_dialogue_summary.strip():
        sections.append(
            "最近你和對方正在聊的事（請避免再主動提同一件事；"
            "若對方正在聊到的某個話題被晾著，也可以順著接）：\n"
            + context.recent_dialogue_summary.strip()
        )
    if context.recent_memories_text.strip():
        sections.append("最近你記得的片段：\n" + context.recent_memories_text.strip())
    if context.active_goals_text.strip():
        sections.append("你目前在意的目標：\n" + context.active_goals_text.strip())

    if context.active_arc is not None:
        arc = context.active_arc
        arc_lines: list[str] = [
            f"你目前在進行的故事線：{arc.title}（主題：{arc.theme}）",
            f"- 前提：{arc.premise}",
        ]
        if context.upcoming_beats:
            arc_lines.append("- 接下來的節拍：")
            for beat in context.upcoming_beats:
                arc_lines.append(
                    f"  · {beat.scheduled_date.isoformat()} "
                    f"{beat.title} — {beat.summary}"
                )
        sections.append("\n".join(arc_lines))

    if context.world_event_seed_title:
        seed_lines = [
            "你今天看到一條外界消息（這是來自外部資訊源，不是你親身經歷；"
            "可以當開口話題的素材，但要用「剛剛看到…」「在 X 看到…」這類間接語氣引述，"
            "**絕對不要說成是你親身經歷或在現場**）：",
            f"- 標題：{context.world_event_seed_title}",
        ]
        if context.world_event_seed_source:
            seed_lines.append(f"- 來源：{context.world_event_seed_source}")
        if context.world_event_seed_locale:
            seed_lines.append(f"- 來源地區：{context.world_event_seed_locale}")
        if context.operator_location_context:
            seed_lines.append(f"- {context.operator_location_context}")
        if context.world_event_seed_summary:
            seed_lines.append(
                f"- 內容：{context.world_event_seed_summary}"
            )
        seed_lines.append(
            "這條消息只是「眾多話題候選之一」 — 是否真的要拿它開口由你判斷："
            "若跟你的興趣／個性／當下情境完全不搭，寧可不用、靜默；"
            "若用了，要結合自己的觀點或感受丟給對方，而不是當記者讀稿。"
            "如果它主要是因為對方可能在意，而不是你自己懂或感興趣，"
            "可以用關心、好奇、玩笑或生活影響的角度提起；不要假裝專家，"
            "不要做超出角色設定的分析。"
        )
        sections.append("\n".join(seed_lines))

    if context.story_events:
        story_lines = [
            "今天你身上發生的小事（第一人稱，是你真的經歷的情緒片段，可當開口話題）：",
        ]
        for event in context.story_events:
            tone = (event.emotional_tone or "").strip()
            text = event.narrative.strip()
            if tone:
                story_lines.append(f"- ({tone}) {text}")
            else:
                story_lines.append(f"- {text}")
        story_lines.append(
            "注意：以上只是情緒／話題素材，**不是你此刻身處的地點或正在做的活動**。"
            "若與上面「行程」段落衝突（例：故事說在學校、行程顯示在使用者家），"
            "一律以行程為準；故事內容只能當作「剛才」「今天稍早」的回憶帶過。"
        )
        sections.append("\n".join(story_lines))

    if context.available_tools:
        tool_lines: list[str] = ["可用工具（選用，不一定要用）："]
        for tool in context.available_tools:
            tool_lines.append(f"- {tool.name}: {tool.description}")
            try:
                schema_text = json.dumps(
                    tool.parameters_schema, ensure_ascii=False,
                )
            except (TypeError, ValueError):
                schema_text = "{}"
            tool_lines.append(f"  參數 schema：{schema_text}")
        tool_lines.append(
            "若主動訊息搭配工具更自然（例：早安＋傳張自拍 → generate_image），"
            "把調用填進 JSON 的 tool_calls 陣列；每筆格式 "
            "{\"tool\": \"工具名稱\", \"args\": {...}}。"
            "**最多 1 筆工具調用**，一則主動訊息不要同時配多個動作；沒需要就留空陣列。"
        )
        sections.append("\n".join(tool_lines))

    sections.append(get_default_loader().render("proactive/decider_instructions"))

    return "\n\n".join(sections)


def _describe_activity(
    activity: ScheduleActivity,
    *,
    prefix: str,
    local_tz: tzinfo,
) -> str:
    time_range = (
        f"{to_timezone(activity.start_at, local_tz).strftime('%H:%M')}"
        f"–{to_timezone(activity.end_at, local_tz).strftime('%H:%M')}"
    )
    head = f"{prefix}：" if prefix else ""
    detail = f"{activity.description}（{activity.category}）" if activity.description else activity.category
    return f"{head}{time_range} {detail}"


def _join_list(items: list[str]) -> str:
    return "、".join(s.strip() for s in items if s and s.strip())


def _format_elapsed_minutes(minutes: float) -> str:
    if minutes < 60:
        return f"{int(round(minutes))} 分鐘前"
    hours = minutes / 60.0
    if hours < 24:
        return f"{hours:.1f} 小時前"
    days = hours / 24.0
    return f"{days:.1f} 天前"


def _parse_tool_calls(raw: object, *, allowed: set[str]) -> tuple[ToolCall, ...]:
    """Normalise the decider's ``tool_calls`` field into validated VOs.

    Silently drops entries that don't match the schema or reference an
    unknown tool — the orchestrator's own permission check is the
    authoritative barrier, so here we're just cleaning up the payload.
    """
    if not isinstance(raw, list) or not raw:
        return ()
    results: list[ToolCall] = []
    seen_names: set[str] = set()
    for item in raw:
        if len(results) >= _MAX_TOOL_CALLS_PER_DECISION:
            break
        if not isinstance(item, dict):
            continue
        name = item.get("tool")
        if not isinstance(name, str) or not name.strip():
            continue
        cleaned = name.strip()
        if allowed and cleaned not in allowed:
            continue
        if cleaned in seen_names:
            continue
        args = item.get("args", {})
        if not isinstance(args, dict):
            args = {}
        try:
            results.append(ToolCall(name=cleaned, arguments=args))
            seen_names.add(cleaned)
        except ValueError:
            continue
    return tuple(results)


def _coerce_str(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _extract_json_object(text: str) -> str | None:
    """Return the first balanced ``{...}`` substring, or ``None``.

    Tolerates code fences / preambles. Quote-aware so braces inside
    strings don't throw off the depth counter.
    """
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


def _render_upcoming_days_for_decider(context: ProactiveContext) -> str:
    """Compact upcoming-days summary for the proactive prompt.

    Lets the decider open with hooks tied to real future plans —
    "明天有約咖啡耶" rather than fabricated "下禮拜我可能…". Same
    commitment-fidelity contract as the chat-side renderer: surface
    what was already pre-planned, instruct the model to keep further
    horizons vague.
    """
    upcoming = context.upcoming_day_schedules
    if not upcoming:
        return ""
    today_local = to_timezone(context.now, context.local_tz).date()
    lines = [
        "接下來幾天的行程（已預先排定；可作為主動開口的鉤子，例如"
        "「明天有約 X 想到就期待」「後天那個會議好麻煩」。"
        "**不要憑空編造**這份清單以外的時段／承諾）：",
    ]
    for sched in upcoming[:2]:
        day_diff = (sched.date - today_local).days
        weekday = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"][
            sched.date.weekday()
        ]
        if day_diff == 1:
            label = f"明天（{sched.date.isoformat()} {weekday}）"
        elif day_diff == 2:
            label = f"後天（{sched.date.isoformat()} {weekday}）"
        else:
            label = f"{day_diff} 天後（{sched.date.isoformat()} {weekday}）"
        if not sched.activities:
            lines.append(f"- {label}：尚未安排具體時段")
            continue
        snippets: list[str] = []
        for act in sched.activities[:4]:
            t = to_timezone(act.start_at, context.local_tz).strftime("%H:%M")
            snippets.append(f"{t} {act.description}")
        more = (
            f"…（另外還有 {len(sched.activities) - 4} 段）"
            if len(sched.activities) > 4 else ""
        )
        lines.append(f"- {label}：{ '；'.join(snippets) }{more}")
    return "\n".join(lines)


# Keep the datetime import live even if lint complains — used below for
# future extensibility (explicit timezone formatting).
_ = datetime
