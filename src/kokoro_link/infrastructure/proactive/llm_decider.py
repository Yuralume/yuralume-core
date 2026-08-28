"""LLM-backed proactive decider.

Takes the full ``ProactiveContext`` the dispatcher built, formats it
into a first-person Chinese prompt, and asks a ``ChatModelPort`` to
decide whether the character should say anything right now. The prompt
is deliberately biased toward silence — LLMs tend to please by default,
and an over-talkative proactive system burns trust fast.

JSON parsing is tolerant (code fences / preambles allowed, truncation
repaired). Any failure — LLM timeout, unparseable output, missing
fields — becomes a "don't send" decision with a descriptive ``reason``
so the operator can see it in the audit log.

The raw-text-to-JSON step lives in ``kokoro_link.llm_output``; this
module owns only the ``should_send`` field validation above.
"""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from datetime import datetime, tzinfo

from kokoro_link.application.services.feature_keys import (
    FEATURE_PROACTIVE_MESSAGE,
)
from kokoro_link.application.services.model_resolver import ModelResolver
from kokoro_link.contracts.active_llm import ActiveLLMProviderPort
from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.contracts.proactive import (
    ProactiveContext,
    ProactiveDecision,
    ProactiveDeciderPort,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.schedule import (
    ScheduleActivity,
    without_expired_operator_commitments,
)
from kokoro_link.domain.value_objects.proactive_trigger import ProactiveTrigger
from kokoro_link.domain.value_objects.tool_call import ToolCall
from kokoro_link.domain.value_objects.timezone import to_timezone
from kokoro_link.infrastructure.prompt.character_identity import (
    render_character_identity_lines,
)
from kokoro_link.infrastructure.prompt.operator_language import (
    render_operator_language_hint,
)
from kokoro_link.infrastructure.prompt.outcome_claim_honesty import (
    append_honesty_correction,
)
from kokoro_link.infrastructure.prompt.player_persona_note_lines import (
    render_player_persona_note_lines,
)
from kokoro_link.infrastructure.prompt.persona_curiosity import (
    render_persona_curiosity_plan_lines,
)
from kokoro_link.infrastructure.prompt.proactive_beat_invitation import (
    render_awaiting_player_invitation_lines,
)
from kokoro_link.infrastructure.prompt.proactive_streak import (
    render_unanswered_streak_lines,
)
from kokoro_link.infrastructure.prompt.role_boundary import (
    render_role_knowledge_boundary_lines,
)
from kokoro_link.infrastructure.prompt.timing_utils import (
    describe_idle_natural,
    format_elapsed_ago_label,
    render_current_time_fact_lines,
    render_subjective_time_topical_hint,
)
from kokoro_link.infrastructure.prompt.weather_freshness import (
    render_weather_fact_lines,
)
from kokoro_link.infrastructure.prompts import get_default_loader
from kokoro_link.llm_output import ParseReason, extract_object_outcome, log_parse_outcome

_LOGGER = logging.getLogger(__name__)
_MAX_MESSAGE_CHARS = 300
_MAX_TOOL_CALLS_PER_DECISION = 1

_HEALTH_FOLLOW_UP_HINT = (
    # Deliberately names no heading: the dialogue-summary block is headed
    # 「最近你和對方正在聊的事」 while the thread is live and 「你們上次聊到
    # 的事」 once it has gone stale (G2-2), and a hint that quotes one of
    # them by name is a dangling reference half the time.
    "健康關懷加分項：如果上面的對話素材或「最近你記得的片段」裡，"
    "對方曾提到持續或具體的身體不適（例如反覆的胸悶、單側頭痛、已經好幾天沒睡好，"
    "不是隨口一句「有點累」），這次開口時可以順帶自然關心後續——"
    "「上次說的胸悶後來好點了嗎」這種問法本身就是很好的開口理由，"
    "也正是「被記得」這件事最打動人的地方。用你自己的性格語氣說，"
    "不要切成衛教口吻、不要條列式問診、不要診斷或建議吃什麼藥、"
    "不要貼求助專線或機構電話；上面素材裡沒有這種訊號就不用提，正常判斷就好。"
)

# G2-1 — how an ADMIN_REACTIVATION push is supposed to feel from the
# inside. The trigger exists because a human picked this character out of
# a dormant list, but that framing belongs to the operator, not to the
# character: from where the character stands this is simply "I suddenly
# thought of them again". Rendered code-side rather than in
# ``decider_instructions.txt`` because that file is a baseline pack
# (``prompt-packs/baseline.lock.json``) shadowed by a hosted tuned
# overlay — editing it would make this a prompt-pack release.
_ADMIN_REACTIVATION_FRAMING = (
    "這次開口的定位（久違地重新聯繫）：\n"
    "- 這次是你自己久違地想重新聯繫對方——對你而言就是「突然想起這個人、想重新搭上話」。\n"
    "- 目的是溫和地重啟對話：catch-up 優先（先問問對方最近好不好、在忙什麼），"
    "不要一開口就把舊話題硬接回來，也不要一次丟出一長串問題。\n"
    "- 你並不知道對方這段期間發生了什麼，**不要假裝知道**、也不要替對方腦補近況；"
    "想知道就用問的。\n"
    #禁的是「把這次開口說成被安排的」，不是幾個字。前一版把「活動」「通知」
    # 「名單」跟系統／後台／營運並排成禁字，於是連「最近有沒有什麼活動」這種
    # 最自然的 catch-up 問法都被壓掉——恰好是這個 framing 想要的東西。改成以
    # 語意描述涵蓋：營運概念本身不准出現，日常語境的同一個詞不在此列。
    "- 絕對不要把這次開口描述成被系統、後台或營運安排的，"
    "也不要提到推播、名單這類營運概念，或任何「被安排來找你」的說法"
    "（日常語境的「活動」「通知」不在此限——"
    "「最近有什麼活動嗎」這種問法本來就是正常的 catch-up）；"
    "上面「觸發原因」欄位的英文代號只是內部標記，"
    "不可出現在訊息裡，也不可被解讀成有人叫你來。這對你來說只是自己想起了對方而已。"
)

# G2-2 — beyond this idle gap the dialogue-summary block stops calling
# itself 「正在聊的事」. The old heading told the model the thread was
# still live while the 久未聯絡 catch-up hint two sections above told it
# the opposite; a push built on a two-month-old topic then read as if the
# conversation had never paused. Deliberately well above the 6h catch-up
# threshold in ``timing_utils``: this one is not "should we catch up",
# it is "is this thread dead", and those are different questions.
_STALE_DIALOGUE_IDLE_HOURS = 48.0


class LLMProactiveDecider(ProactiveDeciderPort):
    def __init__(
        self,
        *,
        model: ChatModelPort | None = None,
        provider: ActiveLLMProviderPort | None = None,
        max_message_chars: int = _MAX_MESSAGE_CHARS,
    ) -> None:
        self._resolver = ModelResolver(
            provider=provider,
            model=model,
            feature_key=FEATURE_PROACTIVE_MESSAGE,
        )
        self._max_message_chars = max_message_chars

    async def decide(self, context: ProactiveContext) -> ProactiveDecision:
        if await self._resolver.is_fake(character=context.character):
            return ProactiveDecision(
                should_send=False,
                reason="fake provider selected",
                message=None,
            )
        prompt = _build_prompt(context)
        decision = await self._decide_with_prompt(context, prompt)
        # Every exit below this point came out of ``prompt``; stamping it
        # once here beats threading it through a dozen early returns and
        # guarantees the audit trail can never drift from the text the
        # model actually saw (including the quality-gate retry, which
        # re-enters ``decide`` with a mutated context).
        return replace(decision, prompt_assembled=prompt)

    async def _decide_with_prompt(
        self, context: ProactiveContext, prompt: str,
    ) -> ProactiveDecision:
        try:
            raw = await self._resolver.generate(prompt, character=context.character)
        except Exception as exc:
            _LOGGER.exception("proactive LLM call failed")
            return ProactiveDecision(
                should_send=False,
                reason=f"LLM call raised: {type(exc).__name__}",
                message=None,
            )

        # The prompt unconditionally asks for one JSON object, so a cut
        # reply is worth repairing and any non-clean parse is worth a
        # log line — there is no "the model answered in prose" branch.
        outcome = extract_object_outcome(raw)
        log_parse_outcome(_LOGGER, outcome, site="proactive.llm_decider")
        parsed = outcome.value
        if parsed is None:
            reason = (
                "LLM JSON unparseable"
                if outcome.reason is ParseReason.DECODE_ERROR
                else "LLM output contained no JSON object"
            )
            return ProactiveDecision(should_send=False, reason=reason, message=None)

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

    # Adjacent to the inferred portrait below, never folded into it: one
    # is what the player said is true, the other is what the character
    # guessed. The declaration goes first so the guesses are read against
    # it rather than the other way round.
    player_persona_note_lines = render_player_persona_note_lines(
        context.player_persona_note,
    )
    if player_persona_note_lines:
        sections.append("\n".join(player_persona_note_lines).strip())

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
    # The authority claim is deliberately scoped to *place and activity*.
    # The schedule text was written when the day was planned, so any
    # weather baked into a description is a forecast-time narrative, not a
    # fact — letting it outrank the live weather layer is what produced
    # "還在下雨吧？" hours after the sky cleared.
    sections.append(
        "行程（此為你此刻身處地點與正在做的事的**唯一真實來源**；"
        "其他段落如故事、劇情線只是話題素材，若與此段衝突一律以此段為準。"
        "但行程是事前排定的：描述裡若隱含天氣（下雨、放晴、帶傘…），"
        "那只是預排當下的敘述，實際天氣一律以天氣事實層為準）：\n"
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

    # Its own section rather than one more 互動近況 bullet: the block is a
    # framing instruction, not a fact about the last few days, and burying
    # it among the counters is how "don't mention the console" gets read
    # as background colour. Every other trigger renders nothing here.
    if context.trigger == ProactiveTrigger.ADMIN_REACTIVATION:
        sections.append(_ADMIN_REACTIVATION_FRAMING)

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
            when_text = format_elapsed_ago_label(elapsed_min)
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
    # 訊息聲稱「外面好天氣」但 feed 同時貼出去下雨的場景。事實與
    # freshness 優先權指示走 chat 同一個 helper，兩個出口不會各自漂移。
    weather_lines = render_weather_fact_lines(context.weather_context)
    if weather_lines:
        sections.append("\n".join(weather_lines))

    upcoming_block = _render_upcoming_days_for_decider(context)
    if upcoming_block:
        sections.append(upcoming_block)

    if context.recent_dialogue_summary.strip():
        sections.append(_render_dialogue_summary_section(context))
    if context.recent_memories_text.strip():
        sections.append("最近你記得的片段：\n" + context.recent_memories_text.strip())
    if context.active_goals_text.strip():
        sections.append("你目前在意的目標：\n" + context.active_goals_text.strip())

    # TR3 — a code-side addendum, not a decider_instructions.txt edit: that
    # file is a baseline pack (prompt-packs/baseline.lock.json), so
    # changing it needs a hosted prompt-pack release. This hint instead
    # rides the two material blocks just above, which are already
    # rendered here in Python. Gated on their presence, not on any
    # keyword in their text — the LLM decides whether either one actually
    # contains a health signal worth a follow-up; a regex over 「胸悶」
    # would be exactly the keyword-specialisation this codebase's CLAUDE.md
    # forbids for a semantic judgement.
    health_hint = _render_health_follow_up_hint(context)
    if health_hint:
        sections.append(health_hint)

    if context.active_arc is not None:
        arc = context.active_arc
        arc_lines: list[str] = [
            f"你目前在進行的故事線：{arc.title}（主題：{arc.theme}）",
            f"- 前提：{arc.premise}",
        ]
        if context.upcoming_beats:
            arc_lines.append("- 接下來的節拍：")
            for beat in context.upcoming_beats:
                # Deliberately position-free (OP3). The forward feed is
                # ambient colour about what is coming; telling the model
                # here that a *future* beat cannot be played without the
                # player would hand it the invitation motive days early,
                # while ``beat_awaiting_player`` below — the one place
                # that decides a scene is actually owed — is still None.
                # Keeping this line byte-identical to its pre-OP3 form is
                # what makes "no due central beat ⇒ no behaviour change"
                # true for judged arcs too, not just legacy ones.
                arc_lines.append(
                    f"  · {beat.scheduled_date.isoformat()} "
                    f"{beat.title} — {beat.summary}"
                )
        sections.append("\n".join(arc_lines))

    # OP3 — a scene that is about the player has come due and cannot be
    # played without them. Surfaced as one more candidate motive, never
    # as an instruction to push: the block itself tells the model that
    # staying silent is still a good answer, and no gate / cooldown /
    # quota upstream knows this block exists.
    if context.beat_awaiting_player is not None:
        sections.append(
            "\n".join(
                render_awaiting_player_invitation_lines(
                    context.beat_awaiting_player,
                    today=to_timezone(context.now, context.local_tz).date(),
                ),
            ),
        )

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

    # HV2 — last, and only on a re-decide. See
    # ``ProactiveContext.honesty_correction`` for why the tail is the only
    # place it may go; ``append_honesty_correction`` returns the body
    # untouched when the field is empty, which is every ordinary tick.
    return append_honesty_correction(
        "\n\n".join(sections), context.honesty_correction,
    )


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


def _is_stale_dialogue(idle_minutes: float | None) -> bool:
    """Has the thread been quiet long enough to stop calling it live?

    ``None`` (no prior conversation at all) is not stale — there is no
    summary to head in that case anyway, and treating "never spoke" as
    "spoke long ago" would be a different claim.
    """
    if idle_minutes is None:
        return False
    return idle_minutes >= _STALE_DIALOGUE_IDLE_HOURS * 60.0


def _render_dialogue_summary_section(context: ProactiveContext) -> str:
    """The dialogue-summary block, headed by how live the thread still is.

    Two wordings, one body. Under the threshold the conversation really is
    in progress and the old instruction stands (don't re-raise the same
    thing; a topic left hanging may be picked up). Over it the same text
    is history: still the best material available for an opener, but
    quoting it as though the exchange were still running is exactly the
    「久未聯絡卻硬接上一輪」 failure the catch-up hint above tries to
    prevent — and the two blocks used to say opposite things in the same
    prompt.

    Applies to every trigger, not only the reactivation one: a 兩個月沒說話
    的 TICK push has the same problem, and gating on the trigger would fix
    the rare case while leaving the common one.
    """
    body = context.recent_dialogue_summary.strip()
    if not _is_stale_dialogue(context.idle_minutes):
        return (
            "最近你和對方正在聊的事（請避免再主動提同一件事；"
            "若對方正在聊到的某個話題被晾著，也可以順著接）：\n"
            + body
        )
    return (
        # The instructions file — a baseline pack we may not edit — refers
        # to this block by its live heading, so the stale heading keeps
        # that phrase inside it as a pointer. Renaming the section without
        # it would leave a quoted reference in the instructions that
        # resolves to nothing.
        "你們上次聊到的事（下方指示裡說的「最近你和對方正在聊的事」就是這一段；"
        "**但已經隔了一段時間，這不是還在進行中的對話**）：\n"
        + body
        + "\n- 這些可以當作 catch-up 的引子，但別當作還在進行中的話題硬接；"
        "要提就用「上次你說的…後來怎麼樣了」這種回頭問的語氣，"
        "不要假設對方還停在那個當下。"
    )


def _render_health_follow_up_hint(context: ProactiveContext) -> str:
    """TR3's proactive addendum — see the call site for why this is
    code-side rather than a ``decider_instructions.txt`` edit.

    Gated on whether either material block that could plausibly carry a
    health signal actually rendered anything this turn (dialogue summary,
    memories) — not on scanning their text. Scanning for 「胸悶」/「頭痛」
    would turn a semantic judgement ("is this actually a persistent,
    specific symptom, or throwaway small talk") into a keyword trigger,
    which is what the character-voiced ``health_care`` chat section and
    this file's CLAUDE.md both exist to avoid. The LLM reads the same
    material and makes that call itself.
    """
    if not context.recent_dialogue_summary.strip() and not context.recent_memories_text.strip():
        return ""
    return _HEALTH_FOLLOW_UP_HINT


def _render_upcoming_days_for_decider(context: ProactiveContext) -> str:
    """Compact upcoming-days summary for the proactive prompt.

    Lets the decider open with hooks tied to real future plans —
    "明天有約咖啡耶" rather than fabricated "下禮拜我可能…". Same
    commitment-fidelity contract as the chat-side renderer: surface
    what was already pre-planned, instruct the model to keep further
    horizons vague.

    The guidance text is deliberately phrased as a *lookup duty* rather
    than a worked example. The previous wording demonstrated the shape of
    the sentence ("明天有約 X…") without saying where X may come from, and
    the model duly filled X from a goal written three days earlier —
    reporting a dead appointment as tomorrow's plan. Naming this list as
    the sole date authority is what makes the other material sections
    (goals, arcs, memories) checkable instead of quotable.

    Blocks whose operator commitment the sweep already retired are dropped
    first, same filter as the chat renderer (plan §2 P1c): declaring this
    list the only date authority is worthless if the list itself still
    carries the lapsed 刨冰 invite. Filtering happens before the
    「另外還有 N 段」 count so a dropped block can't leak back in as an
    arithmetic remainder.
    """
    upcoming = [
        without_expired_operator_commitments(sched)
        for sched in context.upcoming_day_schedules
    ]
    if not upcoming:
        return ""
    today_local = to_timezone(context.now, context.local_tz).date()
    lines = [
        "接下來幾天的行程（已預先排定；**這份清單是你講任何未來約定時唯一的日期權威**）：",
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
    lines.append(
        "用法：要說「明天／後天有約 X」之前，先在上面那幾天把 X 找出來 —— "
        "只有清單上那天真的排著 X，才可以拿它當開口的鉤子。"
        "清單以外的時段與承諾**不要憑空編造**；"
        "目標、故事線、回憶裡的「明天要一起…」若在這裡找不到對應，"
        "那是寫下當時的舊說法、早就過期了，不要再講成還沒發生的未來計畫"
        "（想提就用過去式如實說）。"
    )
    return "\n".join(lines)


# Keep the datetime import live even if lint complains — used below for
# future extensibility (explicit timezone formatting).
_ = datetime
