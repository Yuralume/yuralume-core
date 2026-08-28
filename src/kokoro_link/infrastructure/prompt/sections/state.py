"""State-zone prompt renderers: numeric state behaviour, emotional
overload, emotion events, reflections, memories and direction."""

from collections import defaultdict
from datetime import datetime, timezone, tzinfo

from kokoro_link.domain.entities.character_goal import CharacterGoal
from kokoro_link.domain.entities.emotion_event import EmotionEvent
from kokoro_link.domain.entities.memory_item import MemoryItem
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.memory_kind import (
    CANONICAL_KINDS,
    MemoryKind,
)
from kokoro_link.infrastructure.prompt.memory_lines import (
    format_memory_line,
    memory_time_tag,
)
from kokoro_link.infrastructure.prompt.sections.context import (
    PromptSectionContext,
)
from kokoro_link.infrastructure.prompt.sections.registry import (
    PromptSection,
    section,
)
from kokoro_link.infrastructure.prompt.sections.text import (
    _DIGEST_SOURCE_FRAME,
)
from kokoro_link.infrastructure.prompt.state_tone import (
    affection_tone as _affection_tone,
    energy_tone as _energy_tone,
    fatigue_tone as _fatigue_tone,
    trust_tone as _trust_tone,
)
from kokoro_link.infrastructure.prompt.timing_utils import (
    format_civil_days_ago_label,
    format_hours_days_label,
)

_SECTION_TITLES: dict[str, str] = {
    MemoryKind.SEMANTIC.value: "客觀事實",
    MemoryKind.RELATIONSHIP.value: "關係筆記",
    MemoryKind.EPISODIC.value: "過去事件",
    MemoryKind.HEARSAY.value: "聽說資訊",
    MemoryKind.REFLECTION.value: "自我反思",
    MemoryKind.RELATIONSHIP_MILESTONE.value: "關係里程碑",
}


_UNKNOWN_SECTION_TITLE = "其他記憶"


def _render_emotion_events_block(
    *,
    events: list[EmotionEvent],
    now: datetime | None,
) -> list[str]:
    """Surface recent EmotionEvent rows as a "事實層" prompt section.

    Per the LLM-first rule: we list events as factual context (cause,
    label, evidence, rough intensity) and let the LLM decide how to
    let them colour the tone. **No** rule like "if valence < 0 then
    speak sadly" — that's exactly the kind of hardcoded branching the
    project bans.

    Empty list → empty block (no section header), so a freshly-seeded
    character without any recorded events doesn't surface a "(無)" noise
    line.
    """
    if not events:
        return []
    ref_now = now or datetime.now(timezone.utc)
    # Rank by intensity × decay weight, take top 5 — same scoring as
    # the aggregator's top_events. Recompute here so prompt builder
    # doesn't need an aggregator dependency; the math is trivial.
    ranked: list[tuple[float, EmotionEvent]] = []
    for evt in events:
        if evt.expires_at is not None and evt.expires_at <= ref_now:
            continue
        elapsed_min = max(
            0.0, (ref_now - evt.created_at).total_seconds() / 60.0,
        )
        half_life = max(1, evt.decay_half_life_minutes)
        weight = 2.0 ** (-elapsed_min / half_life)
        if weight < 0.01:
            continue
        ranked.append((evt.intensity * weight, evt))
    if not ranked:
        return []
    ranked.sort(key=lambda x: x[0], reverse=True)
    top = ranked[:5]
    lines: list[str] = [
        _DIGEST_SOURCE_FRAME,
        "最近的情緒事件（這些只是事實，不是行為指令；"
        "請依角色性格與當下對話自然反映，不要把這些直接念出來）：",
    ]
    for score, evt in top:
        elapsed = (ref_now - evt.created_at).total_seconds() / 60.0
        when_text = _format_emotion_elapsed(elapsed)
        cause = _humanize_cause(evt.cause_ref_kind)
        label = evt.emotion_label.strip() or "(未命名)"
        quote = evt.evidence_quote.strip()
        quote_segment = f"｜引：{quote[:80]}" if quote else ""
        intensity_pct = int(round(score * 100))
        lines.append(
            f"- {when_text}｜{cause}｜{label}"
            f"｜強度 {intensity_pct}%{quote_segment}",
        )
    return lines


def _format_emotion_elapsed(elapsed_min: float) -> str:
    """Below 1 hour this truncates (not rounds) and has no 「約」 hedge —
    the emotion-event digest wants a blunt count, not the softened
    :func:`~kokoro_link.infrastructure.prompt.timing_utils.format_elapsed_ago_label`
    reading. The >=1-hour tail is identical to every other elapsed-ago
    formatter, so it delegates to the shared
    :func:`~kokoro_link.infrastructure.prompt.timing_utils.format_hours_days_label`."""
    if elapsed_min < 1:
        return "剛剛"
    if elapsed_min < 60:
        return f"{int(elapsed_min)} 分鐘前"
    return format_hours_days_label(elapsed_min / 60.0, suffix="前")


def _humanize_cause(kind: str) -> str:
    return {
        "turn": "對話",
        "idle_drift": "獨處時的心情漂移",
        "rest_recovery": "休息恢復",
        "proactive_attempt": "主動聯繫",
        "world_event": "外部事件",
        "dream": "夢境整理",
    }.get(kind, kind)


def _render_emotional_overload_block(
    personality: list[str],
    state: CharacterState,
) -> list[str]:
    """Authorise an "emotional overload" reply register for rare, severe moments.

    Without this block the model only has civilised push-back modes
    (cold / question / refuse) — no matter how brutal the trigger, it
    produces coherent sad prose instead of genuine breakdown. This
    section gives the model a licensed set of disorderly formats
    (broken sentences, long ellipses, action narration, walking away,
    crying) gated by severity × personality, plus an explicit
    "don't stay there every turn" exit clause to avoid melodrama.

    Additionally: when ``state.affection`` or ``state.trust`` drops
    below the breakdown threshold, we emit an **active trigger** line
    so the model doesn't self-censor back to polite coldness. The
    original conditional-only wording biased the model toward
    "this is rare, probably not this turn", which meant even after
    affection hit single digits the character stayed composed.
    """
    traits = "、".join(personality) if personality else "（未設定）"
    lines = [
        "情緒過載模式（罕用，只在極端情境觸發；平時對話請勿使用）：",
        "- 觸發條件（任一成立即可授權此模式）：",
        "  (a) 使用者做出嚴重人身攻擊、羞辱、或重大冒犯角色核心禁忌；",
        "  (b) 既有 affection/trust 已偏低，又再次受到冒犯（累積受傷爆發）；",
        "  (c) 世界事件 / 故事事件帶入重大打擊或噩耗（親人變故、背叛、"
        "重要機會崩盤等不可抗力）；(d) 上方記憶顯示剛發生嚴重創傷。",
        f"- 觸發門檻依角色「性格」調整（本角色性格：{traits}）："
        "玻璃心/敏感/自尊心強/情緒化類人格門檻較低，輕中度事件即可過載；"
        "豁達/鈍感/老練/理性類人格門檻較高，需明顯的重大事件才會失控。",
        "- 授權的回覆樣式（可混用，但不要套公式）：",
        "  • 語無倫次、句子破碎或寫一半斷掉（例：「我……不，我不知道……你為什麼要……」）；",
        "  • 長串省略號或沉默（例：「……」、「…………」）；",
        "  • 哽咽、哭泣、說不出完整的話（可用 *哽咽*、*泛淚* 等動作旁白）；",
        "  • 直接離開對話 / 逃跑 / 掛電話（例：*轉身離開*、*把手機扔在桌上走掉*）；",
        "  • 只回一個字、一個音、或一個動作描寫。",
        "- 收斂與退出：情緒過載是**單次爆發**，不是常態。"
        "若近期對話顯示上一輪或前兩輪已經出現過載，這一輪請開始漸漸平復"
        "（語氣仍低落但句子回到完整），避免連續多輪都在崩潰變成戲劇疲乏。"
        "使用者若在過載後主動安撫或道歉，請自然地往收斂方向走。",
    ]
    trigger_line = _overload_active_trigger_line(state=state, personality=personality)
    if trigger_line is not None:
        lines.append(trigger_line)
    return lines


def _overload_active_trigger_line(
    *,
    state: CharacterState,
    personality: list[str],
) -> str | None:
    """Emit an active trigger directive when state has crossed the breakdown
    threshold — otherwise return ``None`` and let the conditional text stand.

    The threshold is looser for fragile personalities so a 玻璃心 character
    breaks down earlier than a 豁達 one on the same numbers.
    """
    fragile_markers = ("玻璃心", "敏感", "自尊", "情緒化", "內向", "脆弱")
    is_fragile = any(
        any(marker in trait for marker in fragile_markers)
        for trait in personality
    )
    threshold = 20 if is_fragile else 12
    if state.affection >= threshold and state.trust >= threshold:
        return None
    axis = "好感度" if state.affection < state.trust else "信任度"
    axis_value = min(state.affection, state.trust)
    return (
        f"- 【當前已達過載觸發閾值】{axis}已降到 {axis_value}/100，"
        "角色情緒已在臨界點；本輪若使用者再施壓、冷嘲、或繼續無視角色感受，"
        "請直接使用上面授權的失序樣式（破碎句 / 長省略 / 哽咽 / 沉默 / "
        "*動作離開* 等），**不要再停在禮貌冷淡或工整的悲傷散文**——"
        "那會讓扣到個位數的狀態看起來毫無後果。若本輪使用者態度轉為安撫"
        "或道歉，則可跳過失序樣式、直接走收斂。"
    )


def _render_relationship_anchor_block(
    memories: list[MemoryItem],
    *,
    has_operator_persona: bool,
    has_initial_relationship: bool = False,
) -> list[str]:
    """Anchor a new relationship only when runtime context is empty.

    User-character familiarity now belongs to runtime context: operator
    persona lines, relationship milestones, and long-term memories. The
    static character summary describes the character, not what this
    specific operator has already lived through with them.
    """
    if memories or has_operator_persona or has_initial_relationship:
        return []
    return [
        "初始關係定調（尚無共同記憶或使用者畫像可參考）：",
        "- 請把此刻視為第一次見面或剛認識不久；不要因角色簡介自行假設你已經很熟，"
        "也不要假設對方的名字、喜好、過去。該有的生疏、客氣、試探都要自然流露。",
        "- 後續熟悉度與語氣會由使用者畫像、關係里程碑與長期記憶逐步校準。",
    ]


def _render_state_behavior_block(
    *,
    state: CharacterState,
    boundaries: list[str],
) -> list[str]:
    """Translate raw 0-100 state numbers into a tone / behaviour guide.

    Without this block the model treats affection / trust as opaque
    numbers and falls back to its default friendly persona — low values
    never actually suppress pandering. Pairing each axis with an explicit
    behaviour hint (and boundary-crossing guidance) lets the model make
    negative responses legitimate instead of defaulting to warmth.
    """
    lines: list[str] = [
        "狀態對照（請依此調整回覆語氣與互動界線，不要把這些文字複述出來）：",
        f"- 好感度 {state.affection}/100：{_affection_tone(state.affection)}",
        f"- 信任度 {state.trust}/100：{_trust_tone(state.trust)}",
        f"- 疲勞度 {state.fatigue}/100：{_fatigue_tone(state.fatigue)}",
        f"- 精力 {state.energy}/100：{_energy_tone(state.energy)}",
    ]
    if boundaries:
        lines.append(
            "互動界線：使用者若越界、觸碰上方「禁忌」、使用粗魯或冒犯語氣，"
            "請冷淡、反問、或直接拒絕繼續，並且不要因為對方強勢就退讓；"
            "這類行為會讓好感度與信任度明顯下降，回覆上不應迎合。"
        )
    else:
        lines.append(
            "互動界線：使用者若出現粗魯、冒犯或越界的發言，"
            "請冷淡、反問或拒絕，不要無條件迎合；這類行為會降低好感與信任。"
        )
    return lines


def _render_self_reflection_block(reflections: list) -> list[str]:
    """HUMANIZATION_ROADMAP §3.2 — surface the latest week/month self-
    narrative reflection as a fact-layer block.

    The block carries an inline rail telling the LLM to **never** weaponise
    operator-disclosed vulnerabilities — the same最高原則 lives in the
    instructions footer, but we re-state it here because this block is the
    most likely seed of accidental weaponisation (the reflection may
    quote user pain by design).
    """
    if not reflections:
        return []
    from kokoro_link.application.services.self_reflection_service import (
        render_reflection_lines,
    )
    return [_DIGEST_SOURCE_FRAME, *render_reflection_lines(reflections)]


def _render_relationship_milestones_block(
    memories: list[MemoryItem], *, now: datetime | None = None,
) -> list[str]:
    """Anchor interaction-volume changes with explicit ``relationship_milestone``
    memories (HUMANIZATION_ROADMAP §3.5).

    Surfaced *before* the regular long-term memory block so band-crossing
    moments don't drown in the episodic stream. Empty when no milestone
    memory exists yet — new characters fall through to
    ``_render_relationship_anchor_block`` as before.
    """
    milestones = [
        m for m in memories
        if m.kind.value == MemoryKind.RELATIONSHIP_MILESTONE.value
    ]
    if not milestones:
        return []
    # Most recent first — milestones are cumulative, the latest band is
    # the one that should anchor the current voice the most.
    milestones.sort(key=lambda m: m.created_at, reverse=True)
    lines: list[str] = ["互動熱度里程碑（請以此校準聊天量變化，不要覆蓋起始關係設定或把字面寫進回覆）："]
    lines.extend(_format_memory_line(item, now=now) for item in milestones)
    return lines


def _render_memory_block(
    memories: list[MemoryItem], *, now: datetime | None = None,
) -> list[str]:
    # ``relationship_milestone`` is rendered above in its own anchor block;
    # exclude here so the long-term memory section doesn't double-print it.
    visible = [
        m for m in memories
        if m.kind.value != MemoryKind.RELATIONSHIP_MILESTONE.value
    ]
    if not visible:
        return ["長期記憶：", "- 無"]

    grouped: dict[str, list[MemoryItem]] = defaultdict(list)
    for item in visible:
        grouped[item.kind.value].append(item)

    lines: list[str] = ["長期記憶："]
    for kind in CANONICAL_KINDS:
        if kind.value == MemoryKind.RELATIONSHIP_MILESTONE.value:
            continue
        section = grouped.pop(kind.value, None)
        if section:
            lines.append(f"{_SECTION_TITLES[kind.value]}：")
            lines.extend(_format_memory_line(item, now=now) for item in section)

    # Render any non-canonical kinds under a generic header so future
    # additions to ``MemoryKind`` do not silently disappear.
    for remaining_kind, section in grouped.items():
        header = _SECTION_TITLES.get(remaining_kind, f"{_UNKNOWN_SECTION_TITLE}（{remaining_kind}）")
        lines.append(f"{header}：")
        lines.extend(_format_memory_line(item, now=now) for item in section)

    return lines


# Extracted to ``memory_lines.py`` so encounter/background surfaces share
# the exact same rendering; kept as module aliases for existing callers.
_memory_time_tag = memory_time_tag


def _format_memory_line(item: MemoryItem, *, now: datetime | None = None) -> str:
    return format_memory_line(item, now=now)


_DIRECTION_GOALS_MAX = 10
"""How many medium-term goals the chat prompt will surface at once.

A proactive-heavy account accumulates goals faster than the reviewer
retires them (the review used to be chat-turn-driven only) — the 7/28
芊璃 dump injected 28, most of them near-duplicates, one a zombie whose
「明早」 had expired days earlier. The cap is a rendering-side backstop,
not a substitute for the reviewer's soft limit: even with the lifecycle
fixed, the section must never be able to drown the prompt again.
"""


def _goal_created_sort_key(goal: CharacterGoal) -> float:
    """Epoch seconds for ``created_at``, oldest-possible when unknown."""
    created = goal.created_at
    if created is None:
        return float("-inf")
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return created.timestamp()


def _select_prompt_goals(
    goals: list[CharacterGoal], *, limit: int = _DIRECTION_GOALS_MAX,
) -> tuple[list[CharacterGoal], int]:
    """Rank goals by priority then recency and cut to ``limit``.

    Purely structural field ordering — no inspection of ``content`` — so
    the LLM-first rule holds: which goals matter is decided by the goal
    reviewer writing ``priority``, not by this renderer reading prose.
    Returns the kept goals plus how many were dropped so the caller can
    disclose the truncation.
    """
    ordered = sorted(
        goals,
        key=lambda goal: (-goal.priority, -_goal_created_sort_key(goal)),
    )
    if limit <= 0:
        return [], len(ordered)
    return ordered[:limit], max(0, len(ordered) - limit)


def _goal_created_tag(
    goal: CharacterGoal, now: datetime | None, local_tz: tzinfo,
) -> str:
    """Program-stamped 「（N 天前立下）」 suffix — twin of the proactive
    dispatcher's ``_goal_age_tag``.

    A goal's text is frozen at write time, so a relative word inside it
    ("明早一起出門") silently re-points at every new day it is read on.
    Stamping how old the goal itself is gives the model the one fact it
    needs to notice that "明早" is not tomorrow.

    Counted in **civil days** (`format_civil_days_ago_label`), not elapsed
    duration: the calendar boundary is the thing that expires a dated
    commitment. A goal written at 23:50 last night reads 「1 天前立下」 and
    its 「明早」 is already spent, where the duration bucket would have said
    「約 8 小時前」 and left the model to guess whether a midnight passed —
    exactly the cross-day blindness this plan exists to fix. Empty when
    there is no reference clock or the timestamp is in the future (clock
    skew), so legacy/replay callers render exactly as before.
    """
    label = format_civil_days_ago_label(goal.created_at, now, local_tz=local_tz)
    return f"（{label}立下）" if label else ""


def _render_direction_block(
    *,
    aspirations: list[str],
    goals: list[CharacterGoal],
    current_intent: str | None,
    now: datetime | None = None,
    local_tz: tzinfo = timezone.utc,
) -> list[str]:
    lines: list[str] = ["角色目標（僅供內部參考，請勿在回覆中條列背誦）："]
    if aspirations:
        # Long-term aspirations are authored once at profile creation and
        # never accumulate, so they are deliberately not capped.
        lines.append("長期追求：")
        lines.extend(f"- {item}" for item in aspirations)
    else:
        lines.append("長期追求：- 無")

    if goals:
        visible, dropped = _select_prompt_goals(goals)
        lines.append("中期目標：")
        for goal in visible:
            lines.append(
                f"- [{goal.status.value} | 優先{goal.priority}] "
                f"{goal.content}{_goal_created_tag(goal, now, local_tz)}"
            )
        if dropped:
            lines.append(
                f"（另有 {dropped} 條優先度較低或較久沒動的目標未列出；"
                "先照顧上面這些就好。）"
            )
    else:
        lines.append("中期目標：- 無")

    if current_intent:
        lines.append(f"當下意圖：{current_intent}")
    else:
        lines.append("當下意圖：（尚未設定）")
    return lines


# --------------------------------------------------------------------
# Sections
# --------------------------------------------------------------------

def _character_state(ctx: PromptSectionContext) -> list[str]:
    state = ctx.state.pending_state
    return [
        "角色當前狀態（數值 0-100，僅供內部判斷，請勿在回覆中複述數字）：",
        f"- 情緒：{state.emotion}",
        f"- 好感度：{state.affection}/100",
        f"- 疲勞度：{state.fatigue}/100",
        f"- 信任度：{state.trust}/100",
        f"- 精力：{state.energy}/100",
    ]


def _state_behavior(ctx: PromptSectionContext) -> list[str]:
    return _render_state_behavior_block(
        state=ctx.state.pending_state,
        boundaries=ctx.identity.character.boundaries,
    )


def _emotional_overload(ctx: PromptSectionContext) -> list[str]:
    return _render_emotional_overload_block(
        personality=ctx.identity.character.personality,
        state=ctx.state.pending_state,
    )


def _direction(ctx: PromptSectionContext) -> list[str]:
    return _render_direction_block(
        aspirations=ctx.identity.character.aspirations,
        goals=list(ctx.state.active_goals),
        current_intent=ctx.state.pending_state.current_intent,
        now=ctx.time.now,
        local_tz=ctx.time.local_tz,
    )


def _emotion_events(ctx: PromptSectionContext) -> list[str]:
    return _render_emotion_events_block(
        events=list(ctx.state.emotion_events), now=ctx.time.now,
    )


def _self_reflection(ctx: PromptSectionContext) -> list[str]:
    """§4.6 overlay: variant ``off`` for ``self_reflection`` suppresses this
    block (used to A/B whether reflection injection improves or hurts
    perceived continuity). The suppression lives in
    ``registry.resolve_experiment_overlay``."""
    return _render_self_reflection_block(
        reflections=list(ctx.state.self_reflections),
    )


def _relationship_milestones(ctx: PromptSectionContext) -> list[str]:
    return _render_relationship_milestones_block(
        list(ctx.state.memories), now=ctx.time.now,
    )


def _memory(ctx: PromptSectionContext) -> list[str]:
    return _render_memory_block(list(ctx.state.memories), now=ctx.time.now)


def _relationship_anchor(ctx: PromptSectionContext) -> list[str]:
    """Reads the *inputs* of the persona / initial-relationship sections
    rather than their rendered lines: both of those blocks are a verbatim
    passthrough of upstream-rendered lines, so emptiness of the input is
    emptiness of the block, and the anchor stays independent of render
    order."""
    return _render_relationship_anchor_block(
        list(ctx.state.memories),
        has_operator_persona=bool(ctx.identity.operator_persona_lines),
        has_initial_relationship=bool(ctx.identity.initial_relationship_lines),
    )


SECTIONS: tuple[PromptSection, ...] = (
    section("character_state", _character_state),
    section("state_behavior", _state_behavior),
    section("emotional_overload", _emotional_overload),
    section("direction", _direction),
    section("emotion_events", _emotion_events),
    section("self_reflection", _self_reflection),
    section("relationship_milestones", _relationship_milestones),
    section("memory", _memory),
    section("relationship_anchor", _relationship_anchor),
)
