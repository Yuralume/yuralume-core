"""Default prompt builder.

Renders character profile, current state, recent turns, grouped
long-term memories, the character's aspirations/goals/intent, today's
scheduled activities (current + upcoming), and the latest user message
into a Chinese plain-text prompt. The project is Chinese-first; writing
the scaffolding in Chinese stops the model from echoing English section
headers or narrative tags in its reply (classic prompt-bleed symptom
with Gemma-family models).
"""

from collections import defaultdict
from dataclasses import fields, is_dataclass
from datetime import date as date_type, datetime, timezone, tzinfo
from typing import Mapping

import json

from kokoro_link.contracts.prompt import (
    PromptContextBuilderPort,
    PromptToolDescriptor,
    ToolOutcomeMessage,
)
from kokoro_link.contracts.clock import ClockPort, ensure_utc
from kokoro_link.contracts.persona_curiosity import PersonaCuriosityPlan
from kokoro_link.contracts.prompt_material_digest import PromptMaterialDigest
from kokoro_link.contracts.register_profile import RegisterProfile
from kokoro_link.contracts.reply_quality import ReplyDiversityEvidence
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.character_goal import CharacterGoal
from kokoro_link.domain.entities.conversation import (
    Conversation,
    Message,
    MessageKind,
    MessageRole,
)
from kokoro_link.domain.entities.emotion_event import EmotionEvent
from kokoro_link.domain.entities.feed_post import FeedPost
from kokoro_link.domain.entities.memory_item import MemoryItem
from kokoro_link.domain.entities.proactive_attempt import ProactiveAttempt
from kokoro_link.domain.entities.schedule import DailySchedule, ScheduleActivity
from kokoro_link.domain.entities.operator_persona import OperatorPersona
from kokoro_link.domain.entities.operator_profile import OperatorProfile
from kokoro_link.domain.entities.story_arc import StoryArc, StoryArcBeat
from kokoro_link.domain.entities.story_event import StoryEvent
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.content_flow import (
    CONTENT_TOLERANCE_FRONTIER,
    sanitize_messages_for_tolerance,
)
from kokoro_link.domain.value_objects.presence_frame import (
    AccessContext,
    ChatSurface,
    PresenceFrame,
    VisibilityMode,
)
from kokoro_link.domain.value_objects.memory_kind import CANONICAL_KINDS, MemoryKind
from kokoro_link.domain.value_objects.timezone import timezone_for_id, to_timezone
from kokoro_link.infrastructure.localization.fallback_texts import (
    localized_fallback_text,
)
from kokoro_link.infrastructure.prompt.character_identity import (
    render_character_identity_lines,
)
from kokoro_link.infrastructure.prompt.operator_language import (
    render_operator_language_lines,
)
from kokoro_link.infrastructure.prompt.role_boundary import (
    render_role_knowledge_boundary_lines,
)
from kokoro_link.infrastructure.prompt.state_tone import (
    affection_tone as _affection_tone,
    energy_tone as _energy_tone,
    fatigue_tone as _fatigue_tone,
    trust_tone as _trust_tone,
)
from kokoro_link.infrastructure.prompt.timing_utils import (
    describe_idle_natural,
    format_gap_duration_label,
    format_relative_past_label,
    render_current_time_fact_lines,
    render_subjective_time_topical_hint,
    time_of_day_hint,
)
from kokoro_link.infrastructure.prompt.memory_lines import (
    format_memory_line,
    memory_time_tag,
)
from kokoro_link.infrastructure.prompt.register_blocks import (
    render_diversity_evidence_block,
    render_turn_register_block,
)
from kokoro_link.infrastructure.prompts import get_default_loader

LATEST_USER_MESSAGE_MARKER = "最新使用者訊息："
"""Marker used by ``FakeChatModel`` to locate the latest user message.

Exposed as a module constant so callers that need to parse the rendered
prompt (tests, fake provider) do not hard-code the string.
"""

_SECTION_TITLES: dict[str, str] = {
    MemoryKind.SEMANTIC.value: "客觀事實",
    MemoryKind.RELATIONSHIP.value: "關係筆記",
    MemoryKind.EPISODIC.value: "過去事件",
    MemoryKind.HEARSAY.value: "聽說資訊",
    MemoryKind.REFLECTION.value: "自我反思",
    MemoryKind.RELATIONSHIP_MILESTONE.value: "關係里程碑",
}
_UNKNOWN_SECTION_TITLE = "其他記憶"
_ROLE_LABELS: dict[str, str] = {"user": "使用者", "assistant": "角色"}
_DIGEST_SOURCE_FRAME = (
    "以下是事實參照，不是文體範本；不要模仿其措辭、句式或意象。"
)


def _format_marker_prefix(numbers: list[int]) -> str:
    """``[1, 2]`` → ``"[圖 1][圖 2] "`` (trailing space included so
    callers can splice straight in front of the message text)."""
    if not numbers:
        return ""
    return "".join(f"[圖 {n}]" for n in numbers) + " "


def _render_vision_ownership_lines(
    markers: "Mapping[int, list[int]]",
    messages: list[Message],
) -> list[str]:
    """One line per ``[圖 N]`` naming who sent it.

    The vision inventory carries history images across turns, so the
    character must be able to tell its own earlier send (whose content
    it already "knows") apart from what the user just attached —
    otherwise it reacts to its own image as if the user sent it.
    Index ``len(messages)`` is the current user turn's slot.
    """
    current_turn = len(messages)
    entries: list[tuple[int, str]] = []
    for turn_idx, numbers in markers.items():
        if turn_idx == current_turn:
            source = "使用者這一輪剛傳來的圖"
        elif (
            0 <= turn_idx < len(messages)
            and messages[turn_idx].role.value == "assistant"
        ):
            source = "你自己稍早傳給對方的圖（內容你本來就知道）"
        else:
            source = "使用者稍早傳來的圖"
        entries.extend((number, source) for number in numbers)
    entries.sort()
    return [f"- [圖 {number}]：{source}" for number, source in entries]


def _render_image_recognition_block(context: str) -> list[str]:
    """Wrap the multimodal recognition summary for a text-only main model.

    Rendered in the prompt body next to the ``圖片標記`` legend (see the
    call site) — the closing guard line scopes any illegible-photo-text
    wording to the photo itself so the model doesn't tease the user
    about an "unreadable message".
    """
    cleaned = (context or "").strip()
    if not cleaned:
        return []
    return [
        "[圖片識別摘要：以下由系統的多模態模型產生，依 [圖 N] 順序"
        "描述上述圖片的畫面內容，供目前純文字模型理解圖片；"
        "這是系統提供的背景資料，不是使用者傳的文字。]",
        cleaned,
        "[/圖片識別摘要]",
        "（摘要若略過或看不清圖中某些小字，那只是照片細節的限制，"
        "與對方訊息本身無關；不要因此評論對方訊息難懂或難讀。）",
    ]


def _format_history_line(message: Message, marker_numbers: list[int]) -> str:
    """Render one ``role：text`` line, prefixing any ``[圖 N]`` markers
    that belong to this turn so the model can tell which of the
    images it received goes with which historical message."""
    role_label = _ROLE_LABELS.get(message.role.value, message.role.value)
    prefix = _format_marker_prefix(marker_numbers)
    return f"{role_label}：{prefix}{message.content}"


_HISTORY_GAP_MARKER_THRESHOLD_MINUTES = 6 * 60.0
"""Insert a time-gap separator between two consecutive history turns (or
between the last turn and the current message) when their authored-time
gap exceeds this. 6h matches the subjective-time catch-up boundary in
``timing_utils`` — below it the turns read as one continuous sitting,
above it the model should see that time passed. Without this the literal
last line ("我要去買飲料" sent yesterday afternoon) reads as a live
message when the user returns the next morning, because the flat
transcript carries no per-turn time."""


def _message_created_at(message: Message) -> datetime | None:
    created = getattr(message, "created_at", None)
    if created is None:
        return None
    if created.tzinfo is None:
        return created.replace(tzinfo=timezone.utc)
    return created


def _format_history_gap_marker(gap_minutes: float, *, trailing: bool) -> str:
    label = format_gap_duration_label(gap_minutes)
    if trailing:
        return f"——（距離上面這幾句已經隔了{label}，以下才是這次的新訊息）——"
    return f"——（中間隔了{label}）——"


def _render_history_lines(
    messages: list[Message],
    markers: "Mapping[int, list[int]]",
    *,
    now: datetime | None = None,
    gap_threshold_minutes: float = _HISTORY_GAP_MARKER_THRESHOLD_MINUTES,
) -> list[str]:
    """Render the "近期對話" transcript, interleaving time-gap separators.

    A separator is inserted whenever the gap between two consecutive
    turns crosses ``gap_threshold_minutes`` so multi-sitting / multi-day
    windows don't read as one continuous thread. After the loop, when
    ``now`` is known and the last turn predates it by the same
    threshold, a trailing seam marker is appended so the stale last line
    isn't read as a just-now message (the current turn is rendered below
    the transcript as ``最新使用者訊息``). Negative gaps (clock skew /
    default timestamps from replay) never fire."""
    lines: list[str] = []
    previous_at: datetime | None = None
    for idx, message in enumerate(messages):
        current_at = _message_created_at(message)
        if previous_at is not None and current_at is not None:
            gap = (current_at - previous_at).total_seconds() / 60.0
            if gap >= gap_threshold_minutes:
                lines.append(_format_history_gap_marker(gap, trailing=False))
        lines.append(_format_history_line(message, markers.get(idx, [])))
        if current_at is not None:
            previous_at = current_at
    if now is not None and previous_at is not None:
        gap = (now - previous_at).total_seconds() / 60.0
        if gap >= gap_threshold_minutes:
            lines.append(_format_history_gap_marker(gap, trailing=True))
    return lines


class DefaultPromptContextBuilder(PromptContextBuilderPort):
    def __init__(
        self,
        *,
        humanization_settings=None,  # noqa: ANN001
        prompt_quality_settings=None,  # noqa: ANN001
        local_tz: tzinfo = timezone.utc,
        clock: ClockPort | None = None,
    ) -> None:
        """Create a prompt builder.

        ``humanization_settings`` is intentionally optional so isolated
        unit tests and legacy wiring keep the default "all enabled"
        behaviour. Runtime container wiring passes AppSettings.humanization.
        """
        self._humanization_settings = humanization_settings
        self._prompt_quality_settings = prompt_quality_settings
        self._local_tz = local_tz
        self._clock = clock
        self.last_prompt_pack_hash = get_default_loader().prompt_pack_hash(
            prompt_pack_hash_snapshot(
                humanization_settings,
                prompt_quality_settings,
            ),
        )

    def _humanization_enabled(
        self, field_name: str, *, default: bool = True,
    ) -> bool:
        settings = self._humanization_settings
        if settings is None:
            return default
        return bool(getattr(settings, field_name, default))

    def build(
        self,
        *,
        character: Character,
        conversation: Conversation,
        recent_messages: list[Message],
        memories: list[MemoryItem],
        pending_state: CharacterState,
        latest_user_message: str,
        active_goals: list[CharacterGoal] | None = None,
        current_activity: ScheduleActivity | None = None,
        upcoming_activities: list[ScheduleActivity] | None = None,
        just_finished_activity: ScheduleActivity | None = None,
        completed_today_activities: list[ScheduleActivity] | None = None,
        pending_invite_activities: list[ScheduleActivity] | None = None,
        now: datetime | None = None,
        idle_minutes: float | None = None,
        available_tools: list[PromptToolDescriptor] | None = None,
        tool_outcomes: list[ToolOutcomeMessage] | None = None,
        forced_tool_name: str | None = None,
        story_events: list[StoryEvent] | None = None,
        story_arc: "StoryArc | None" = None,
        upcoming_arc_beats: "list[StoryArcBeat] | None" = None,
        today_local: "date_type | None" = None,
        older_dialogue_summary: str | None = None,
        vision_markers: "Mapping[int, list[int]] | None" = None,
        image_recognition_context: str = "",
        recent_proactive_messages: "tuple[ProactiveAttempt, ...] | None" = None,
        recent_feed_posts: "tuple[FeedPost, ...] | None" = None,
        self_repetition_hint: str | None = None,
        phrase_habit_lines: list[str] | None = None,
        presence_frame: "PresenceFrame | None" = None,
        operator: "OperatorProfile | None" = None,
        operator_persona_lines: "list[str] | None" = None,
        peer_roster_lines: "list[str] | None" = None,
        initial_relationship_lines: "list[str] | None" = None,
        persona_curiosity_plan: PersonaCuriosityPlan | None = None,
        calendar_context: str = "",
        weather_context: str = "",
        world_event_context: "tuple[str, ...] | None" = None,
        upcoming_day_schedules: "list[DailySchedule] | None" = None,
        emotion_events: "list | None" = None,
        self_reflections: "list | None" = None,
        address_preference=None,  # noqa: ANN001 - optional, type checked at use
        experiment_overlay: "dict[str, str] | None" = None,
        content_tolerance: str = CONTENT_TOLERANCE_FRONTIER,
        material_digest: PromptMaterialDigest | None = None,
        turn_register_profile: RegisterProfile | None = None,
        reply_diversity_evidence: ReplyDiversityEvidence | None = None,
        retry_directive: str | None = None,
        resolved_player_address: "ResolvedAddress | None" = None,
        resolved_character_address: "ResolvedAddress | None" = None,
        address_change_lines: "list[str] | None" = None,
    ) -> str:
        # ``vision_markers`` carries the cross-turn image inventory:
        # which 1-based ``[圖 N]`` tags belong to which turn. The
        # caller (``ChatService``) computes it once via
        # ``_build_vision_inventory`` so the placeholder ordering in
        # the prompt matches the order of ``image_urls`` sent to the
        # model. Index ``len(recent_messages)`` is the special "this
        # turn's user message" slot — we splice its markers next to
        # the latest-user-message line below.
        markers = vision_markers or {}
        self.last_prompt_pack_hash = get_default_loader().prompt_pack_hash(
            prompt_pack_hash_snapshot(
                self._humanization_settings,
                self._prompt_quality_settings,
            ),
        )
        ref_now = ensure_utc(now) if now is not None else (
            self._clock.now() if self._clock is not None else None
        )
        prompt_recent_messages = sanitize_messages_for_tolerance(
            recent_messages,
            content_tolerance=content_tolerance,
        )
        history_lines = _render_history_lines(
            prompt_recent_messages, markers, now=ref_now,
        )
        latest_user_markers = markers.get(len(prompt_recent_messages), [])
        latest_user_prefix = (
            _format_marker_prefix(latest_user_markers)
            if latest_user_markers else ""
        )
        local_tz = _operator_timezone(operator, self._local_tz)
        state_behavior_block = _render_state_behavior_block(
            state=pending_state,
            boundaries=character.boundaries,
        )
        emotional_overload_block = _render_emotional_overload_block(
            personality=character.personality,
            state=pending_state,
        )
        birthday_block = _render_birthday_block(
            character=character, today=today_local,
        )
        knowledge_boundary_block = _render_knowledge_boundary_block()
        residue_block = _render_residue_block(memories=memories, now=ref_now)
        relationship_milestones_block = _render_relationship_milestones_block(
            memories, now=ref_now,
        )
        memory_block = _render_memory_block(memories, now=ref_now)
        relationship_anchor_block: list[str] = []
        direction_block = _render_direction_block(
            aspirations=character.aspirations,
            goals=active_goals or [],
            current_intent=pending_state.current_intent,
        )
        timing_block = _render_timing_block(
            now=ref_now,
            idle_minutes=idle_minutes,
            local_tz=local_tz,
            include_catchup_hint=(
                self._humanization_enabled("subjective_time_enabled")
                and (experiment_overlay or {}).get("subjective_time") != "off"
            ),
        )
        calendar_block = _render_calendar_block(calendar_context)
        weather_block = _render_weather_block(weather_context)
        world_event_context_block = _render_world_event_context_block(
            world_event_context or (),
        )
        schedule_block = _render_schedule_block(
            current=current_activity,
            upcoming=upcoming_activities or [],
            just_finished=just_finished_activity,
            local_tz=local_tz,
        )
        completed_today_block = _render_completed_today_block(
            completed=completed_today_activities or [],
            just_finished=just_finished_activity,
            local_tz=local_tz,
        )
        pending_invites_block = _render_pending_invites_block(
            pending=pending_invite_activities or [],
            local_tz=local_tz,
        )
        upcoming_days_block = _render_upcoming_days_block(
            upcoming_day_schedules or [],
            today_local=today_local,
            local_tz=local_tz,
        )
        tools_block = _render_tools_block(
            available_tools or [], forced_tool_name=forced_tool_name,
        )
        tool_outcomes_block = _render_tool_outcomes_block(tool_outcomes or [])
        story_events_block = _render_story_events_block(story_events or [])
        # Today's scripted beat — directive segment, distinct from the
        # narrative material in ``story_events_block``. Empty for
        # characters without active arcs or beats lacking scene
        # structure (legacy arcs, gacha-only days).
        today_scene_block = _render_today_scene_directive_block(
            arc=story_arc, today=today_local,
        )
        story_arc_block = _render_story_arc_block(
            arc=story_arc,
            upcoming=upcoming_arc_beats or [],
            today=today_local,
        )
        arc_history_block = _render_arc_history_block(story_arc)
        older_dialogue_block = _render_older_dialogue_summary_block(
            older_dialogue_summary,
        )
        operator_block = _render_operator_block(operator, resolved_player_address)
        # Latest per-pair rename, rendered upstream by ``ChatService`` from
        # the address-change log (one event per direction). Surfaces right
        # under the operator identity so the character acknowledges the new
        # term while linking older references to the same person.
        address_change_block: list[str] = list(address_change_lines or [])
        operator_language_block = _render_operator_language_block(operator)
        # operator_persona_lines is built upstream by
        # ``OperatorPersonaService.render_for_prompt`` so this module
        # doesn't have to know about ProfileField shapes / layer
        # rules — keeps the prompt builder a pure formatter and the
        # service free to evolve its rendering policy.
        operator_persona_block: list[str] = list(operator_persona_lines or [])
        peer_roster_block: list[str] = list(peer_roster_lines or [])
        initial_relationship_block: list[str] = list(initial_relationship_lines or [])
        relationship_anchor_block = _render_relationship_anchor_block(
            memories,
            has_operator_persona=bool(operator_persona_block),
            has_initial_relationship=bool(initial_relationship_block),
        )
        recent_proactive_block = _render_recent_proactive_block(
            attempts=recent_proactive_messages or (),
            now=ref_now,
            idle_minutes=idle_minutes,
        )
        recent_self_lines_block = _render_recent_self_lines_block(
            recent_messages=prompt_recent_messages,
        )
        self_repetition_block = _render_self_repetition_hint_block(
            hint=self_repetition_hint,
        )
        phrase_habit_block = _render_phrase_habit_block(phrase_habit_lines or [])
        turn_register_block = _render_turn_register_block(turn_register_profile)
        diversity_evidence_block = _render_diversity_evidence_block(
            reply_diversity_evidence,
        )
        persona_self_check_block = (
            _render_persona_self_check_block()
            if operator_persona_block else []
        )
        persona_curiosity_block = _render_persona_curiosity_block(
            persona_curiosity_plan,
        )
        recent_feed_block = _render_recent_feed_block(
            posts=recent_feed_posts or (),
            now=ref_now,
        )
        material_digest_block = _render_material_digest_block(material_digest)
        presence_frame_model = presence_frame or PresenceFrame.web_stage()
        presence_frame_block = _render_presence_frame_block(
            presence_frame_model,
            operator_language=getattr(operator, "primary_language", None),
        )
        instructions_footer = get_default_loader().render(
            "chat/instructions_footer",
            response_format_instruction=_render_response_format_instruction(
                presence_frame_model,
            ),
        )
        retry_directive_block = _render_retry_directive_block(retry_directive)
        # HUMANIZATION_ROADMAP §4.6 — sticky-bucket overlay, used as a
        # flat ``{key: variant_id}`` lookup that lets specific blocks
        # collapse (``off``) when an experiment variant suppresses them.
        # Resolved once here so all downstream rendering reads the same
        # snapshot — important when an experiment toggles two blocks at
        # once and we don't want a race between them.
        overlay = experiment_overlay or {}
        emotion_events_block = _render_emotion_events_block(
            events=emotion_events or [],
            now=ref_now,
        )
        # §4.6 overlay: variant ``off`` for ``self_reflection`` suppresses
        # the §3.2 block (used to A/B whether reflection injection improves
        # or hurts perceived continuity).
        if overlay.get("self_reflection") == "off":
            self_reflection_block: list[str] = []
        else:
            self_reflection_block = _render_self_reflection_block(
                reflections=self_reflections or [],
            )
        if material_digest_block:
            emotion_events_block = []
            self_reflection_block = []
            story_events_block = []
            story_arc_block = []
            recent_feed_block = []
        # 內在動機傾向（四維 qualitative band）—— 全 medium 時 to_prompt_lines
        # 回空 list，所以不需要額外的 if-else 跳過。LLM-first 紅線：禁止
        # 在這層或下游 heuristic 讀 disposition 的個別欄位做分支條件。
        disposition_block = character.disposition.to_prompt_lines()
        personality_type_block = character.personality_type.to_prompt_lines()
        identity_block = render_character_identity_lines(character)
        # HUMANIZATION_ROADMAP §4.1 — 具身訊號（hunger / thirst / sleep_debt /
        # seasonal_allergy）。全 low 時跳過渲染；非 low 維度自然體現於語氣，
        # **禁止**程式分支讀取（owner decision 2026-05-21）。
        # §4.6: an active experiment with variant id ``off`` for the
        # ``body_state`` key suppresses this block — same shape used for
        # subjective time / self-reflection above.
        if (
            not self._humanization_enabled("body_state_enabled")
            or overlay.get("body_state") == "off"
        ):
            body_state_block = []
        else:
            body_state_block = character.body_state.to_prompt_lines()
        # HUMANIZATION_ROADMAP §4.2 — operator register / address preference.
        # Owner decision (2026-05-21): observation takes priority over the
        # §3.6 explicit pace knob; falls back to pace_preference when the
        # observation buffer is empty.
        register_block = _render_register_block(
            character=character,
            address_preference=(
                address_preference
                if self._humanization_enabled("address_preference_enabled")
                else None
            ),
            resolved_character_address=resolved_character_address,
        )
        # Tell the model what the ``[圖 N]`` markers in history mean.
        # Only emitted when at least one marker exists so a vision-less
        # turn doesn't get a useless explainer. The inventory carries
        # history images too (including the character's own earlier
        # sends), so the legend maps every marker to its sender instead
        # of claiming everything was attached this turn.
        vision_legend_block: list[str] = []
        if markers:
            total = sum(len(v) for v in markers.values())
            vision_legend_block = [
                f"圖片標記：下方對話中共有 {total} 張圖片附件，"
                f"以 [圖 1]、[圖 2] … 依序標記；[圖 N] 出現在哪一則訊息，"
                "就代表那張圖是隨那則訊息附上的，"
                "可自然地參照「剛才那張」「上一張裡的那個」等指涉。",
                *_render_vision_ownership_lines(
                    markers, prompt_recent_messages,
                ),
            ]
        # Recognition summary (text-only main model) renders HERE — in
        # the body, adjacent to the legend — never appended after the
        # instruction footer. Tail placement made its analyst register
        # and OCR hedges the last tokens before generation, and the
        # model role-played them as "the user's message is hard to
        # read" (turn record 9b094fad, 2026-07-15).
        image_recognition_block = _render_image_recognition_block(
            image_recognition_context,
        )
        return "\n".join(
            [
                *operator_language_block,
                *operator_block,
                *address_change_block,
                *operator_persona_block,
                *peer_roster_block,
                *presence_frame_block,
                "角色設定：",
                f"- 名稱：{character.name}",
                f"- 簡介：{character.summary}",
                *identity_block,
                f"- 外觀：{character.appearance or '（未設定）'}",
                f"- 性格：{', '.join(character.personality) or '無'}",
                f"- 興趣：{', '.join(character.interests) or '無'}",
                f"- 說話風格：{character.speaking_style}",
                f"- 禁忌：{', '.join(character.boundaries) or '無'}",
                *disposition_block,
                *personality_type_block,
                *body_state_block,
                *register_block,
                *turn_register_block,
                *phrase_habit_block,
                *birthday_block,
                *knowledge_boundary_block,
                *residue_block,
                "角色當前狀態（數值 0-100，僅供內部判斷，請勿在回覆中複述數字）：",
                f"- 情緒：{pending_state.emotion}",
                f"- 好感度：{pending_state.affection}/100",
                f"- 疲勞度：{pending_state.fatigue}/100",
                f"- 信任度：{pending_state.trust}/100",
                f"- 精力：{pending_state.energy}/100",
                *state_behavior_block,
                *emotional_overload_block,
                *direction_block,
                # Today's scripted scene goes above timing/schedule so
                # the directive lands while the model is still reading
                # high-priority guidance, not buried beneath logistics.
                *today_scene_block,
                *timing_block,
                *calendar_block,
                *weather_block,
                *world_event_context_block,
                *schedule_block,
                *completed_today_block,
                *pending_invites_block,
                *upcoming_days_block,
                *material_digest_block,
                *emotion_events_block,
                *self_reflection_block,
                *tools_block,
                *tool_outcomes_block,
                *story_events_block,
                *story_arc_block,
                *arc_history_block,
                f"對話 ID：{conversation.id}",
                *vision_legend_block,
                *image_recognition_block,
                *older_dialogue_block,
                *recent_proactive_block,
                *recent_feed_block,
                *recent_self_lines_block,
                *self_repetition_block,
                *diversity_evidence_block,
                *persona_self_check_block,
                *persona_curiosity_block,
                "近期對話：",
                *history_lines,
                *relationship_milestones_block,
                *memory_block,
                *initial_relationship_block,
                *relationship_anchor_block,
                f"{LATEST_USER_MESSAGE_MARKER}{latest_user_prefix}{latest_user_message}",
                *retry_directive_block,
                instructions_footer,
            ]
        )


def _render_persona_curiosity_block(
    plan: PersonaCuriosityPlan | None,
) -> list[str]:
    if plan is None or not plan.should_ask:
        return []
    lines = [
        "自然認識對方的提示：",
        "- 如果本輪回覆自然適合，可以把下列探索意圖融入角色自己的語氣；不要把這段當成固定問句。",
        "- 探索不必用問句收尾；也可以先分享你自己的相關經驗或反應，讓對方自然接話。",
        "- 一則回覆最多一個自然問題；若使用者正在求助、情緒高壓或有明確任務，先回應當下，不急著探索。",
        "- 不要提到使用者畫像、資料蒐集、補欄位或問卷；不要列問題清單。",
    ]
    if plan.target_layer is not None:
        lines.append(f"- 目標層級：Layer {plan.target_layer}")
    if plan.target_topic:
        lines.append(f"- 探索主題：{_clip(plan.target_topic, 100)}")
    if plan.tone_strategy:
        lines.append(f"- 語氣策略：{_clip(plan.tone_strategy, 140)}")
    if plan.question_intent:
        lines.append(f"- 探索意圖：{_clip(plan.question_intent, 260)}")
    if plan.safety_reason:
        lines.append(f"- 安全理由：{_clip(plan.safety_reason, 260)}")
    avoid = [_clip(item, 140) for item in plan.avoid if item and item.strip()]
    if avoid:
        lines.append("- 避免：")
        lines.extend(f"  - {item}" for item in avoid[:6])
    return lines


def _render_material_digest_block(
    digest: PromptMaterialDigest | None,
) -> list[str]:
    if digest is None or not digest.bullets:
        return []
    lines: list[str] = [
        "近期素材事實摘要（已去除原文文體；只作事實參照）：",
        "最高原則：若摘要提到使用者曾揭露的脆弱面，必須以保護姿態對待，禁止情勒、禁止當笑點、禁止當籌碼。",
        "行程對齊：以下是今天稍早或近期回憶素材，不是你此刻所在地點或正在做的事；若與「行程」段衝突，一律以行程為準。",
        _DIGEST_SOURCE_FRAME,
    ]
    for bullet in digest.bullets[:12]:
        text = bullet.strip()
        if text:
            lines.append(f"- {_clip(text, 220)}")
    return lines


def _render_retry_directive_block(retry_directive: str | None) -> list[str]:
    feedback = (retry_directive or "").strip()
    if not feedback:
        return []
    return [
        "上一輪嘗試的問題：",
        _clip(feedback, 320),
        "本輪務必帶出至少一件具體的新事、細節、想法或反應；避開上述問題，不要只是把近況素材換句話重講。",
    ]


def prompt_pack_hash_snapshot(
    settings: object | None,
    prompt_quality_settings: object | None = None,
) -> dict[str, object]:
    return {
        "humanization": _settings_snapshot(settings),
        "prompt_quality": _settings_snapshot(prompt_quality_settings),
    }


def _settings_snapshot(settings: object | None) -> dict[str, object]:
    if settings is None:
        return {"defaults": True}
    if is_dataclass(settings):
        return {
            field.name: getattr(settings, field.name)
            for field in fields(settings)
        }
    names = (
        "relationship_milestone_enabled",
        "disposition_drift_enabled",
        "self_reflection_enabled",
        "behavioral_pattern_enabled",
        "deferred_intent_enabled",
        "route_b_enabled",
        "body_state_enabled",
        "subjective_time_enabled",
        "address_preference_enabled",
    )
    return {
        name: getattr(settings, name)
        for name in names
        if hasattr(settings, name)
    }


def _clip(value: str, limit: int) -> str:
    text = " ".join((value or "").strip().split())
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def _render_presence_frame_block(
    presence_frame: "PresenceFrame | None",
    operator_language: str | None = None,
) -> list[str]:
    frame = presence_frame or PresenceFrame.web_stage()
    uses_texting_style = _presence_frame_uses_texting_style(frame)
    # Derive the channel display name from the channel enum honouring the
    # operator's content language (plan #1 / D4) instead of echoing the
    # client-sent natural-language label. Falls back to the frame's own
    # display_name for any channel not in the localized catalogue.
    channel_key = f"presence.channel.{frame.channel.value}"
    try:
        channel_label = localized_fallback_text(channel_key, operator_language)
    except KeyError:
        channel_label = frame.display_name
    lines = [
        "互動語境：",
        f"- 當前介面：{channel_label}（{frame.surface.value} / {frame.channel.value}）。",
    ]
    access_context = frame.access_context
    if access_context is AccessContext.NOT_PLAUSIBLE:
        lines.append(
            "- 使用者目前不合理出現在你的當前場景。請不要描寫使用者已經在你身邊、"
            "房間內、家中或可直接觸碰你。",
        )
        lines.append(
            "- 若需要同場，應先透過文字訊息約定或邀請；本輪請以保持距離的方式自然回應。",
        )
    elif uses_texting_style:
        lines.append(
            "- 這是文字訊息對話：你收到的是對方傳來的訊息，不是面對面場景。",
        )
        lines.append(
            "- 這可能是因為當下不適合直接同場；回覆時避免描寫你直接看見對方、"
            "觸碰對方或和對方面對面做動作。",
        )
    else:
        reason = frame.co_presence_reason or _access_context_label(access_context)
        lines.append(
            f"- 這是站內同場互動；同場理由：{reason}。這個理由是本輪可共處的邊界，請依它自然回應。",
        )
        lines.append(f"- {_access_context_boundary(access_context)}")

    if uses_texting_style:
        lines.extend(_render_texting_style_lines())

    if frame.stage_access_note:
        lines.append(f"- 可抵達性補充：{frame.stage_access_note}")

    if frame.visibility is VisibilityMode.TEXT_AND_ATTACHMENTS:
        lines.append(
            "- 本回合可能含附件；只能依系統實際提供給你的文字與圖片內容回應，"
            "看不到的細節要保留不確定性。",
        )
    elif frame.visibility is VisibilityMode.TEXT_ONLY:
        lines.append(
            "- 本回合只有文字內容；只能依文字與已知記憶理解對方狀態。",
        )
    else:
        lines.append(
            "- 本回合仍以文字為主要輸入；同場感只表示互動框架，不能憑空補完現實細節。",
        )
    return lines


def _presence_frame_uses_texting_style(frame: PresenceFrame) -> bool:
    return (
        frame.access_context is AccessContext.NOT_PLAUSIBLE
        or frame.access_context
        in {AccessContext.TEXT_MESSAGE_ONLY, AccessContext.REMOTE_STAGE}
        or frame.surface is not ChatSurface.WEB_STAGE
    )


def _render_response_format_instruction(frame: PresenceFrame) -> str:
    if _presence_frame_uses_texting_style(frame):
        return (
            "回覆格式慣例：這一輪是手機文字訊息。只輸出對方會在訊息裡看到的文字；"
            "不要寫動作、表情、場景旁白或任何 `*...*` 內容。"
            "如果想帶到你正在做的事，改成口語自然說明，例如「我剛剛在整理相簿」；"
            "不要寫成 `*把手機相簿往下滑*` 這類動作敘事。"
            "自然適合時可用空白行拆成幾則短訊息；不要用 markdown、列表或標題。"
        )
    return (
        "回覆格式慣例：口語台詞直接寫，不要加引號；動作、表情、或當下狀態的描寫請用"
        "**星號 `*...*` 包住**（例：`*倒了杯茶*`、`*偷瞄了一眼*`、`*歪頭*`、"
        "`*沉默片刻*`），讓前端可以把動作/狀態和口語區分渲染。星號 `*...*` 內的"
        "動作、表情與狀態描寫也屬於玩家可見自然語言，必須和台詞一樣使用上方指定的"
        "主要語言；不要因為下方格式範例是中文就把動作描寫寫成中文。若主要語言是 "
        "en-US，動作描寫也應自然寫成英文（例如 `*sets the phone down*`），而不是 "
        "`*放下手機*`。不要改用括號、方括號、破折號或其他符號——全場統一用 "
        "`*...*`，且動作描寫要簡短具體，不要一整段散文包在星號裡。"
    )


def _render_texting_style_lines() -> list[str]:
    return [
        "- 手機即時通訊文體：像在 LINE / IG DM 跟朋友傳訊息；用口語、簡短、自然的句子，"
        "不要一次回一大坨。",
        "- 不要寫動作、表情或場景旁白；不要使用 `*...*` 包動作。只傳你真的會打給對方看的字。",
        "- 訊息密度要依你的內在表達傾向、性格與當下精神狀態決定：多數情況一到兩則就好，"
        "簡短一句也完全可以只傳一則；分享慾低、內向或疲累時通常一兩則就停。",
        "- 通常一到三則；連發四五則以上是少數真的很興奮、分享慾很高、或有很多事想講的時刻。"
        "連發太多會讓對方一直被洗版，請像真人一樣考慮對方來不來得及讀。",
        "- 每則訊息之間空一行。不要為了拆而拆；自然短句優先。",
    ]


def _access_context_label(access_context: AccessContext) -> str:
    return {
        AccessContext.PUBLIC_ENCOUNTER: "公共場所偶遇",
        AccessContext.INVITED_VISIT: "角色邀請或願意接待",
        AccessContext.SCHEDULED_MEETUP: "雙方事先約好的見面",
        AccessContext.ESTABLISHED_ROUTINE: "熟人之間已形成的日常共處慣例",
    }.get(access_context, "可解釋的同場互動")


def _access_context_boundary(access_context: AccessContext) -> str:
    return {
        AccessContext.PUBLIC_ENCOUNTER: "這代表你們在開放或公共場景中合理相遇；不要假設已熟識或能進入私人空間。",
        AccessContext.INVITED_VISIT: "這代表有邀請或接待脈絡；仍需尊重當下活動與角色邊界，不要擴張成無條件進入私領域。",
        AccessContext.SCHEDULED_MEETUP: "這代表有事先約定；可承接約定場景，但不要超出約定內容。",
        AccessContext.ESTABLISHED_ROUTINE: "這代表已有日常共處慣例；可自然互動，但仍需尊重休息、脆弱或高度私密狀態。",
    }.get(access_context, "請依可抵達理由維持互動邊界。")

def _render_operator_language_block(
    operator: "OperatorProfile | None",
) -> list[str]:
    """Thin adapter around the shared helper — keeps the existing
    block-shaped composition in ``build()`` while letting other LLM
    jobs reuse the same wording via
    ``infrastructure.prompt.operator_language``."""
    if operator is None:
        return []
    return render_operator_language_lines(operator.primary_language)


def _render_operator_block(
    operator: "OperatorProfile | None",
    resolved: "ResolvedAddress | None" = None,
) -> list[str]:
    """Render the "對方身份（使用者）" block at the very top of the
    prompt — gives the model a name to attach to the role label
    "使用者" appearing in directives and history lines.

    Phase 1 of the world-system roadmap: when the operator hasn't
    saved a real name yet (``has_real_name() == False``), the block
    renders nothing so legacy "使用者" wording everywhere else still
    reads naturally. Once a real name is saved we surface name +
    aliases + pronouns so the model never has to guess at "他/她"
    pronouns and can pick up the operator by name in cross-character
    contexts later (when other characters appear in memories).

    Multi-character / world phase will extend this with a roster of
    fellow characters; today we only emit the operator's identity.

    When a ``resolved`` address is supplied (the bidirectional address
    resolver, run by the caller with seed + persona + profile in hand),
    its primary term becomes 「稱呼」 and the recognised alternates become
    「別稱」 — so a per-character seed name or learned name outranks the
    global display name, and old names still resolve to the same person.
    Falls back to the legacy display-name rendering when no resolver
    result is passed (keeps non-chat callers untouched)."""
    if resolved is not None and not resolved.is_fallback:
        lines = [
            "對方身份（即角色設定中所說的「使用者」）：",
            f"- 稱呼：{resolved.primary}",
        ]
        if resolved.aliases:
            lines.append(f"- 別稱：{', '.join(resolved.aliases)}")
        if operator is not None and operator.pronouns:
            lines.append(f"- 代名詞：{operator.pronouns}")
        lines.append(
            "在自然對話中可直接用以上稱呼/別稱稱呼對方；"
            "若對方先以特定暱稱自稱，優先使用對方剛剛用的版本。",
        )
        return lines
    if operator is None or not operator.has_real_name():
        return []
    lines = [
        "對方身份（即角色設定中所說的「使用者」）：",
        f"- 稱呼：{operator.display_name}",
    ]
    if operator.aliases:
        lines.append(f"- 別稱：{', '.join(operator.aliases)}")
    if operator.pronouns:
        lines.append(f"- 代名詞：{operator.pronouns}")
    lines.append(
        "在自然對話中可直接用以上稱呼/別稱稱呼對方；"
        "若對方先以特定暱稱自稱，優先使用對方剛剛用的版本。",
    )
    return lines


_BIRTHDAY_SOON_DAYS = 7
"""距離下一次生日 ≤ 這個數字（天）就會在提示裡加上「再 N 天就是生日」
的明示，鼓勵模型自然地帶到準備或期待。再多天的話就只保留靜態欄位
（年齡 / 星座），避免在生日還很遠時硬把話題拉回去。"""


def _render_birthday_block(
    *,
    character: Character,
    today: "date_type | None",
) -> list[str]:
    """Inject the character's age / zodiac / birthday cadence.

    Always-on (static) lines: 出生日期、年齡、星座 — gives the model
    constant background so age-appropriate phrasing emerges naturally
    without us prescribing it.

    Conditional cadence lines: when today *is* the birthday, or the
    next birthday is within the soon-window, an extra directive
    surfaces so the model can lead with the celebration (or quietly
    anticipate it) instead of having to discover the date itself.

    Returns ``[]`` when the operator hasn't set ``date_of_birth`` so
    legacy characters and unfinished imports stay completely
    unaffected.
    """
    if character.date_of_birth is None:
        return []
    if today is None:
        # No reference date means we can't compute age / cadence safely;
        # surface only the raw DOB so the model still has the static
        # context (rare path — caller almost always passes today_local).
        dob = character.date_of_birth
        return [
            "個人資料（生日相關，請自然帶入對話，不要照稿念）：",
            f"- 生日：{dob.month} 月 {dob.day} 日",
        ]
    ctx = character.birthday_context(today)
    if ctx is None:
        return []
    lines = [
        "個人資料（生日相關，請自然帶入對話，不要照稿念）：",
        f"- 生日：{ctx.dob.month} 月 {ctx.dob.day} 日",
        f"- 目前年齡：{ctx.age} 歲（依現實日期推算，會隨時間自然成長）",
        f"- 星座：{ctx.zodiac}",
    ]
    if ctx.is_today:
        lines.append(
            "- 【今天就是你的生日】可以自然地讓對話帶到這件事，"
            "看是想要對方記得、撒嬌、要禮物、低調帶過、還是裝作沒事，"
            "都依角色性格決定；不要刻意提醒對方「今天是我生日」三遍，"
            "也不要在對方主動祝賀前完全裝作不知道。",
        )
    elif 0 < ctx.days_until_next <= _BIRTHDAY_SOON_DAYS:
        lines.append(
            f"- 距離下一次生日還有 {ctx.days_until_next} 天，"
            "可在自然處流露期待、抱怨、計畫、或想要的禮物提示，"
            "但不要每一輪都繞回生日這個話題。",
        )
    lines.append(
        "以上資訊只是讓你知道自己的年齡、生日與星座；星座僅作為閒聊話題，"
        "不是命運導向，請不要把它當成宿命論依據。",
    )
    return lines


_AFTERMATH_TAG = "aftermath"
"""Tag set by :class:`ScheduleMemorializer` on episodic memories whose
LLM-judged residue is worth promoting. The prompt builder uses it (and
nothing else) to decide which memories belong in the 情緒尾韻 block."""

_RESIDUE_FRESH_WINDOW_HOURS = 24
"""How long an aftermath stays in the prime-position block. Past this
window the memory still lives in the regular memory recall path but
stops crowding out the start of the prompt — models psychological
decay: yesterday's annoyance shouldn't pollute today's mood unless the
user brings it up."""


def _render_residue_block(
    *,
    memories: list[MemoryItem],
    now: datetime | None,
) -> list[str]:
    """Promote fresh aftermath memories to a dedicated 情緒尾韻 block.

    Filter rules: memory must carry the ``aftermath`` tag (set by the
    schedule memorialiser when the LLM judged a notable residue) and
    must have been created within the last 24h. Sort newest first so
    the most recent feeling dominates the model's framing of the
    current turn.

    Empty list when no fresh residues — keeps uneventful days lean.
    """
    if not memories:
        return []
    fresh: list[MemoryItem] = []
    for memory in memories:
        if _AFTERMATH_TAG not in memory.tags:
            continue
        if now is None:
            # Caller didn't pass a clock — skip the freshness filter
            # and let every aftermath through. Production callers
            # always pass ``now``; this guards tests / replay paths.
            fresh.append(memory)
            continue
        created = memory.created_at
        if created.tzinfo is None:
            # In-memory test fixtures often produce naive UTC datetimes;
            # treat them as UTC so the freshness window stays correct.
            created = created.replace(tzinfo=timezone.utc)
        elapsed_hours = (now - created).total_seconds() / 3600.0
        if 0.0 <= elapsed_hours <= _RESIDUE_FRESH_WINDOW_HOURS:
            fresh.append(memory)
    if not fresh:
        return []
    # Newest first — most recent feeling dominates the current turn.
    fresh.sort(key=lambda m: m.created_at, reverse=True)
    lines = [
        "最近活動的情緒尾韻（新→舊；這些是你剛經歷的活動還沒散去的感覺，"
        "可以自然地讓對方感覺到，例如語氣帶煩、語氣偏快、想抱怨、心情很好"
        "等等；若話題相關時可以主動帶出來抱怨／分享，但不要照念，"
        "也不要硬背每一條都講一遍）：",
    ]
    for memory in fresh:
        lines.append(f"- {memory.content}")
    return lines


def _render_knowledge_boundary_block() -> list[str]:
    """Authorise the character to admit ignorance / lapses of memory.

    Without this block the LLM defaults to "answer everything
    confidently" — which is fine for a Q&A bot but breaks the illusion
    of a person. People don't know things outside their interests / age
    bracket / life experience, and they don't perfectly recall every
    past conversation. We hand the model the semantic axes (persona /
    age / interests / summary / 過去事件 memories) and let *it* judge
    whether the current question is in-scope, rather than enumerating
    "topics the character should reject" — that path violates the
    project's top directive (no keyword whitelists / hardcoded
    branching).

    Placed right after the birthday block so persona + age + scope are
    read as one coherent unit before the model meets the numeric state.
    """
    return render_role_knowledge_boundary_lines()


def _render_older_dialogue_summary_block(summary: str | None) -> list[str]:
    text = (summary or "").strip()
    if not text:
        return []
    return [
        "較早對話摘要（較舊輪次，系統壓縮）：",
        f"- {text}",
    ]


def _render_recent_proactive_block(
    *,
    attempts: tuple[ProactiveAttempt, ...],
    now: datetime | None,
    idle_minutes: float | None,
) -> list[str]:
    """Surface the character's own recent proactive pushes in the chat prompt.

    Same anti-repetition lever the proactive decider uses — without it
    the chat-side LLM has no idea the character just pinged the user
    on Telegram about the same topic, so a reply that retreads "你今天
    試鏡準備得怎樣了？" right after a proactive that asked the same
    question feels jarring and breaks the illusion of one continuous
    voice across surfaces. We tag whether the user has replied yet so
    unanswered pushes carry extra "back off" weight.
    """
    if not attempts:
        return []
    lines = [
        "你最近主動傳給對方的訊息（新→舊；這些已經送出，"
        "本輪不要再用同樣的題材／問題重問一次，可以換角度或先聽對方說）：",
    ]
    for att in attempts:
        when_text = ""
        reply_tag = ""
        if now is not None:
            elapsed_min = (now - att.decided_at).total_seconds() / 60.0
            when_text = _format_proactive_elapsed(elapsed_min)
            if idle_minutes is not None:
                if idle_minutes < elapsed_min:
                    reply_tag = "（對方已回）"
                else:
                    reply_tag = "（對方還沒回）"
        text = (att.message or "").strip() or "(無內容)"
        prefix = f"- {when_text}{reply_tag}：" if when_text else "- "
        lines.append(f"{prefix}{text}")
    lines.append(
        "若最新一則對方還沒回，更要小心：本輪盡量不要再追問同一件事，"
        "讓對方有空間先回應，或自然轉到別的話題／回應對方剛剛的訊息。"
    )
    return lines


def _render_emotion_events_block(
    *,
    events: list[EmotionEvent],
    now: datetime | None,
) -> list[str]:
    """Surface recent EmotionEvent rows as a "事實層" prompt section.

    Per CLAUDE.md §LLM-first: we list events as factual context (cause,
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
    if elapsed_min < 1:
        return "剛剛"
    if elapsed_min < 60:
        return f"{int(elapsed_min)} 分鐘前"
    hours = elapsed_min / 60.0
    if hours < 24:
        return f"{hours:.1f} 小時前"
    days = hours / 24.0
    return f"{days:.1f} 天前"


def _humanize_cause(kind: str) -> str:
    return {
        "turn": "對話",
        "idle_drift": "獨處時的心情漂移",
        "rest_recovery": "休息恢復",
        "proactive_attempt": "主動聯繫",
        "world_event": "外部事件",
        "dream": "夢境整理",
    }.get(kind, kind)


_SELF_LINES_BUDGET = 3
"""How many recent assistant turns to re-surface as an explicit anti-
repetition rail. Three is enough to catch immediate echo without
crowding the prompt."""

_SELF_LINE_SNIPPET = 180
"""Per-line excerpt budget. Long assistant replies get truncated to a
recognisable tail so the framing stays compact; the full text is still
present in the regular history block below."""


def _render_recent_self_lines_block(
    *,
    recent_messages: list[Message],
) -> list[str]:
    """Re-surface the character's own recent in-conversation replies
    with explicit anti-repetition framing.

    The assistant lines are already in the regular history block, but
    folded in with user turns the model treats them as ambient context
    rather than its own commitments. Repeated phrasing / questions /
    openings within a single conversation are the most common quality
    drop reported by operators — pulling the last few assistant turns
    out into a dedicated rail with "don't reuse these phrasings"
    framing is a cheap, semantic anti-repetition lever (no extra LLM
    call). Same pattern as ``_render_recent_proactive_block`` does for
    cross-surface pushes.

    Returns ``[]`` when there isn't at least one assistant turn yet —
    the rail would just be noise on the very first turn.
    """
    assistant_turns = [
        m for m in recent_messages
        if m.role is MessageRole.ASSISTANT
        and m.kind is MessageKind.CHAT
        and m.content.strip()
    ]
    if not assistant_turns:
        return []
    selected = assistant_turns[-_SELF_LINES_BUDGET:]
    lines: list[str] = [
        "你本對話最近自己說過的話（新→舊；**這些是你自己已經講過的**，"
        "本輪不要再用同樣的措辭、同樣的開場、同樣的提問或同樣的比喻；"
        "若話題沒有變化，可以換切入點或先聽對方說）：",
    ]
    # newest first so the first bullet is the line the model most
    # likely to mechanically echo if not warned.
    for msg in reversed(selected):
        text = msg.content.strip()
        if len(text) > _SELF_LINE_SNIPPET:
            text = text[:_SELF_LINE_SNIPPET] + "…"
        lines.append(f"- {text}")
    return lines


def _render_self_repetition_hint_block(
    *,
    hint: str | None,
) -> list[str]:
    """Surface the periodic self-repetition extractor's verdict.

    Complements ``_render_recent_self_lines_block`` — the lines block
    is a *literal* "you just said these, don't echo them" rail, this
    block is a *semantic* "the pattern across the last 10 turns is X"
    rail. They're cheap together: the lines rail catches immediate
    echo, the hint rail catches slower-forming habits the model
    wouldn't notice from the literal text alone. Empty hint → no
    rail emitted.
    """
    if not hint or not hint.strip():
        return []
    return [
        "你近期回覆中已被偵測到的重複傾向（**本輪請主動避開這些模式**）：",
        hint.strip(),
    ]


def _render_phrase_habit_block(lines: list[str]) -> list[str]:
    habits = [_clip(item, 120) for item in lines if item and item.strip()]
    if not habits:
        return []
    rendered = [
        "角色口吻習慣（來自近期回覆觀察；作為語氣參考，不是固定口頭禪）：",
    ]
    for habit in habits[:3]:
        rendered.append(f"- 可自然延續：{habit}")
    rendered.append(
        "- 不要每句都套用，也不要直接解釋這些觀察；若與角色設定或當下情緒衝突，以當下語境為準。"
    )
    return rendered


# Extracted to ``register_blocks.py`` so encounter/background surfaces share
# the exact same rails; kept as module aliases for existing callers.
_render_turn_register_block = render_turn_register_block
_render_diversity_evidence_block = render_diversity_evidence_block


def _render_persona_self_check_block() -> list[str]:
    return [
        "畫像使用自檢：送出前請檢查本輪是否只是把「關於對方」段落換句話背出來；"
        "若是，請改成回應對方當下訊息，或完全不提畫像資訊。",
    ]


_FEED_PROMPT_SNIPPET_CHARS = 90
"""Cap per-post body in the prompt rail. Long posts crowd out other
context; the snippet is a recall hook, not a faithful reproduction —
the LLM only needs enough to recognise the topic if the user mentions it."""


def _render_recent_feed_block(
    *,
    posts: tuple[FeedPost, ...],
    now: datetime | None,
) -> list[str]:
    """Surface the character's own recent feed-wall posts in chat.

    Without this rail the chat-side LLM has no idea the character just
    posted "今天的咖啡好香" on the feed wall, so when the user opens with
    "你那篇咖啡的動態怎麼了" the character looks blank or — worse —
    invents a different post. We list the most recent posts (newest
    first) with elapsed time + a trimmed snippet so the model can
    recognise references and respond in continuity.
    """
    if not posts:
        return []
    lines = [
        _DIGEST_SOURCE_FRAME,
        "你最近在動態牆上發過的貼文（新→舊；使用者瀏覽時可能會聊到，"
        "本輪請記得自己發過這些內容，不要表現得像沒發過）：",
    ]
    for post in posts:
        when_text = ""
        if now is not None:
            elapsed_min = (now - post.created_at).total_seconds() / 60.0
            when_text = _format_proactive_elapsed(elapsed_min)
        snippet = (post.content_text or "").strip() or "(無內容)"
        if len(snippet) > _FEED_PROMPT_SNIPPET_CHARS:
            snippet = snippet[:_FEED_PROMPT_SNIPPET_CHARS].rstrip() + "…"
        image_tag = "（含圖）" if post.image_url else ""
        prefix = f"- {when_text}{image_tag}：" if when_text else f"- {image_tag}"
        lines.append(f"{prefix}{snippet}")
    return lines


def _format_proactive_elapsed(minutes: float) -> str:
    if minutes < 60:
        return f"{int(round(minutes))} 分鐘前"
    hours = minutes / 60.0
    if hours < 24:
        return f"{hours:.1f} 小時前"
    days = hours / 24.0
    return f"{days:.1f} 天前"


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


def _render_direction_block(
    *,
    aspirations: list[str],
    goals: list[CharacterGoal],
    current_intent: str | None,
) -> list[str]:
    lines: list[str] = ["角色目標（僅供內部參考，請勿在回覆中條列背誦）："]
    if aspirations:
        lines.append("長期追求：")
        lines.extend(f"- {item}" for item in aspirations)
    else:
        lines.append("長期追求：- 無")

    if goals:
        lines.append("中期目標：")
        for goal in goals:
            lines.append(f"- [{goal.status.value} | 優先{goal.priority}] {goal.content}")
    else:
        lines.append("中期目標：- 無")

    if current_intent:
        lines.append(f"當下意圖：{current_intent}")
    else:
        lines.append("當下意圖：（尚未設定）")
    return lines


_REGISTER_PACE_PHRASES: dict[str, str] = {
    "more_active": "對方明確希望你「主動一點 / 多話一點」",
    "balanced": "對方對對話節奏沒有特別偏好",
    "more_quiet": "對方明確希望你「安靜一點 / 多留白」",
}

_REGISTER_FORMALITY_PHRASES: dict[str, str] = {
    "low": "對方說話很放鬆，不太用敬語（暱稱、口語、表情符號常見）",
    "medium": "對方說話的敬語層級中等（禮貌但不過度正式）",
    "high": "對方明顯偏正式（用敬語、不省略主詞、語句完整）",
}

_REGISTER_LENGTH_PHRASES: dict[str, str] = {
    "short": "對方偏好短句、快節奏（一兩句就換話題）",
    "medium": "對方偏好中等長度（句子完整但不冗長）",
    "long": "對方偏好長段、慢慢說明（願意讀完一段話）",
}


def _render_register_block(
    *,
    character: Character,
    address_preference,
    resolved_character_address: "ResolvedAddress | None" = None,
) -> list[str]:
    """HUMANIZATION_ROADMAP §4.2 — operator register / pace fact-layer block.

    Owner decision (2026-05-21): the **observed** ``OperatorAddressPreference``
    (§4.2) takes priority over the **explicit** ``operator_pace_preference``
    knob (§3.6). When both exist the observation leads and the explicit
    setting is demoted to a "secondary cue" bullet — the LLM still sees
    both, just ordered freshest-first.

    Returns an empty list when neither signal is set so the prompt stays
    quiet in the cold-start case.
    """
    observed: list[str] = []
    # Resolved character-direction address (seed > observed salutation)
    # owns the 「對方稱呼你」 slot when it carries a real signal — so an
    # explicit seed surfaces even before any observation. The
    # character-name fallback is intentionally not surfaced so the
    # cold-start prompt stays quiet about an unobserved salutation.
    resolved_salutation = ""
    if (
        resolved_character_address is not None
        and resolved_character_address.provenance.value
        in {"explicit_seed", "observed_preference"}
    ):
        resolved_salutation = resolved_character_address.primary
    has_pref = address_preference is not None and not address_preference.is_empty
    salutation = resolved_salutation or (
        address_preference.salutation if has_pref else ""
    )
    if salutation:
        observed.append(f"- 對方稱呼你：{salutation}")
    if has_pref:
        formality_phrase = _REGISTER_FORMALITY_PHRASES.get(
            address_preference.formality_level,
        )
        if formality_phrase:
            observed.append(f"- {formality_phrase}")
        length_phrase = _REGISTER_LENGTH_PHRASES.get(
            address_preference.response_length_pref,
        )
        if length_phrase:
            observed.append(f"- {length_phrase}")
    pace_phrase = _REGISTER_PACE_PHRASES.get(
        (character.operator_pace_preference or "").strip(),
    )
    if not observed and not pace_phrase:
        return []
    lines = ["對方說話風格與期望節奏（事實層，自然反映於你的回覆）："]
    lines.extend(observed)
    if pace_phrase:
        prefix = "- 〔顯式設定〕" if observed else "- "
        lines.append(f"{prefix}{pace_phrase}")
    return lines


def _render_timing_block(
    *,
    now: datetime | None,
    idle_minutes: float | None,
    local_tz: tzinfo,
    include_catchup_hint: bool = True,
) -> list[str]:
    """Render the "real-time awareness" section.

    Kept optional so callers that don't know ``now`` (older tests,
    rendering against a stored turn) still produce a valid prompt.
    Numbers are rendered in natural language — not raw minutes — so the
    model is less tempted to echo them literally.

    Per HUMANIZATION_ROADMAP §4.4 the topical-layer 久未聯絡 hint is
    appended as its own block (separate from the timing facts above)
    so the LLM can treat catch-up framing independently from the idle
    drift emotional signal. ``include_catchup_hint=False`` lets §4.6
    experiment overlays suppress just the hint while keeping the raw
    timing facts.
    """
    if now is None and idle_minutes is None:
        return []
    lines: list[str] = ["對話時機（僅供內部參考，請勿照字面覆述）："]
    if now is not None:
        lines.extend(
            render_current_time_fact_lines(now, local_tz, heading=None),
        )
    if idle_minutes is not None:
        lines.append(f"- 距離使用者上次發話：{describe_idle_natural(idle_minutes)}")
    if include_catchup_hint:
        topical = render_subjective_time_topical_hint(idle_minutes)
        if topical:
            lines.append("")
            lines.extend(topical)
    return lines


_time_of_day_hint = time_of_day_hint


# ``_describe_idle`` moved to ``timing_utils.describe_idle_natural`` so
# the proactive decider and intention judge can share the same phrasing
# (HUMANIZATION_ROADMAP §4.4). This local name is kept as a thin alias
# for backward compatibility with the older test imports.
_describe_idle = describe_idle_natural


_LOCAL_TZ_WEEKDAY_LABELS = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"]
# Cap on how many upcoming activities we surface per upcoming day so a
# long-tail day (8 activities) doesn't drown out today's plan in the
# prompt budget. Tomorrow shows up to 6; day-after collapses to a one-
# liner regardless of activity count.
_UPCOMING_TOMORROW_MAX = 6


def _render_upcoming_days_block(
    upcoming: "list[DailySchedule]",
    *,
    today_local: "date_type | None",
    local_tz: tzinfo = timezone.utc,
) -> list[str]:
    """Render the rolling-window upcoming-days context.

    Two design constraints:

    1. **Commitment fidelity** — when the user asks "明天有空嗎 / 後天要
       幹嘛", the model must answer from the same plan the planner will
       actually produce on those days. The block surfaces tomorrow at
       moderate detail (time + description) and day-after as a one-
       liner header; the chat model uses these as **commitment hints**
       rather than fabricating.
    2. **Vagueness past the window** — anything ≥ 4 days out is
       intentionally not pre-planned, so we instruct the model to
       admit "還沒安排到那麼遠 / 要看那時候狀況" instead of inventing
       commitments that won't match when the day comes.

    Both branches emit; an empty ``upcoming`` list still renders the
    vagueness instruction so the model knows the rule even when no
    upcoming day is pre-planned yet (cold start, fake provider, etc.).
    """
    lines: list[str] = []
    if upcoming and today_local is not None:
        lines.append(
            "接下來幾天的行程（**這是你已經規劃好的計畫**；使用者問起明天 / 後天時，"
            "請從下面挑出對應時段如實回答；**不要再憑空編造新的時段或承諾**。"
            "若還沒安排到，就明白說「還沒想好」「再看看吧」）："
        )
        for idx, sched in enumerate(upcoming[:2]):
            day_diff = (sched.date - today_local).days
            label = _upcoming_day_label(day_diff, sched.date)
            if idx == 0:
                # Tomorrow — list up to 6 activities with time + description.
                # Skip companions / location detail; commitment-matching
                # only needs the time/event identity.
                acts = list(sched.activities)[:_UPCOMING_TOMORROW_MAX]
                if not acts:
                    lines.append(f"- {label}：尚未安排具體時段。")
                else:
                    lines.append(f"- {label}：")
                    for act in acts:
                        start_local = to_timezone(act.start_at, local_tz).strftime("%H:%M")
                        end_local = to_timezone(act.end_at, local_tz).strftime("%H:%M")
                        lines.append(
                            f"  · {start_local}–{end_local} {act.description}"
                        )
                    if len(sched.activities) > _UPCOMING_TOMORROW_MAX:
                        lines.append(
                            f"  · （另外還有 {len(sched.activities) - _UPCOMING_TOMORROW_MAX} 段未列出）"
                        )
            else:
                # Day-after — one-liner: just the count + the headline
                # activity (longest non-sleep block) so the model has a
                # cheap reference point without the full list.
                headline = _pick_headline_activity(sched)
                if headline is None:
                    lines.append(f"- {label}：尚未安排具體時段。")
                else:
                    h_start = to_timezone(headline.start_at, local_tz).strftime("%H:%M")
                    lines.append(
                        f"- {label}：共 {len(sched.activities)} 段，"
                        f"重點時段 {h_start} {headline.description}。"
                    )
    # Vagueness rail — always emitted so the model has a stable answer
    # for "下禮拜五 / 下個月" questions, regardless of whether tomorrow
    # / day-after were rendered above.
    lines.append(
        "再往後（4 天以後 / 下週 / 下個月）你還沒安排到，被問到具體時段時請說"
        "「還沒想那麼遠」「要看到時候狀況」或「再看看吧」，"
        "**不要憑空編造**會在某個未來日期做什麼事——那會跟之後真正的行程對不上。"
    )
    return lines


def _upcoming_day_label(day_diff: int, when: "date_type") -> str:
    """Human-friendly label for an upcoming day."""
    weekday = _LOCAL_TZ_WEEKDAY_LABELS[when.weekday()]
    if day_diff == 1:
        return f"明天（{when.isoformat()} {weekday}）"
    if day_diff == 2:
        return f"後天（{when.isoformat()} {weekday}）"
    return f"{day_diff} 天後（{when.isoformat()} {weekday}）"


def _pick_headline_activity(
    schedule: "DailySchedule",
) -> "ScheduleActivity | None":
    """Pick the most informative activity on a day for the one-liner.

    Skips sleep / rest categories so the headline is something the
    user can actually anchor a question on ("中午有約咖啡"), and falls
    back to longest non-sleep block when nothing matches.
    """
    if not schedule.activities:
        return None
    informative = [
        a for a in schedule.activities
        if "sleep" not in a.category.lower() and "睡" not in a.category
    ]
    if not informative:
        return schedule.activities[0]
    return max(informative, key=lambda a: a.end_at - a.start_at)


def _render_weather_block(weather_context: str) -> list[str]:
    """Render the real-world weather block (mirrors ``_render_calendar_block``).

    Empty input → no block. The block carries *facts only* (天氣狀況、
    氣溫、降雨機率…), never behavioural directives — LLM-first 紅線。
    The same string is also fed to schedule planner, proactive decider
    and feed composer, so a downpour in chat lines up with "改室內咖
    啡廳" in tomorrow's schedule and won't contradict the feed post
    text.

    We append a *freshness-authority* directive (not a behavioural one):
    the live weather fact is fresh every turn, but the conversation
    history / memory / schedule the model also reads can still be soaked
    in last week's rain. Without telling the model the current fact wins,
    it keeps echoing "記得帶傘" after the sky has cleared. The directive
    only sets precedence (use the current fact, don't continue an
    outdated weather state); it never tells the character how to react to
    the weather, so the LLM-first red line stays intact.
    """
    weather = weather_context.strip()
    if not weather:
        return []
    return [
        weather,
        "（以上為此刻真實天氣事實層。若近期對話、記憶、貼文或行程描述隱含的天氣"
        "與此刻不一致——例如先前在下雨、現在已轉晴——一律以此刻天氣事實為準，"
        "不要延續已經過時的天氣狀態或提醒。）",
    ]


def _render_calendar_block(calendar_context: str) -> list[str]:
    """Render the real-world calendar block.

    The block is produced once per turn by
    :meth:`ScheduleService.describe_calendar` (same string the schedule
    planner sees) so the chat reply and the day's activities stay in
    sync about whether "today" is a workday, a 連假 day, etc. Empty
    input means no calendar provider is wired or context was disabled
    — we emit nothing rather than fabricate a date line.

    Per the project's LLM-first principle: the block delivers *facts
    only* (是否國定假日、是星期幾、屬於什麼連假、季節）— it never tells
    the model "今天不要上班" or "今天要寫早安"; the character persona
    + state + memories drive the actual reaction.
    """
    if not calendar_context.strip():
        return []
    return [
        "今日真實世界行事曆（事實層；學生／上班族／自由工作者該怎麼過今天，"
        "由你依角色設定與性格判斷）：",
        calendar_context.strip(),
    ]


def _render_world_event_context_block(lines: tuple[str, ...]) -> list[str]:
    cleaned = [line.strip() for line in lines if line and line.strip()]
    if not cleaned:
        return []
    return [
        "最近外界事件候選（事實層；來源地區與使用者所在地只供你判斷相關性，"
        "不要當成必須提起的指令）：",
        *cleaned,
    ]


def _render_schedule_block(
    *,
    current: ScheduleActivity | None,
    upcoming: list[ScheduleActivity],
    just_finished: ScheduleActivity | None = None,
    suppress_location: bool = False,
    local_tz: tzinfo,
) -> list[str]:
    """Render the schedule guidance block.

    ``suppress_location`` is set by the caller when the character is
    inside a world (the world-context block is the authoritative source
    of "where am I") — printing schedule.location too would surface a
    second, often-stale location string and the model would oscillate
    between them. The schedule still runs as the *activity* source of
    truth ("正在做什麼"); only the place token is dropped here.
    """
    if current is None and not upcoming and just_finished is None:
        return []
    if suppress_location:
        header = (
            "角色今日行程（此為你**正在做什麼**的唯一真實來源；"
            "你**所在的地點**請參考上方『世界框架 / 此刻所在』段落，"
            "若行程描述與當前世界位置不一致，以世界位置為準）："
        )
    else:
        header = (
            "角色今日行程（此為你此刻身處地點與正在做的事的**唯一真實來源**；"
            "其他段落如故事、記憶、劇情線只是素材，若與本段衝突一律以本段為準；請勿照稿念出）："
        )
    lines: list[str] = [header]

    def _loc(act: ScheduleActivity) -> str:
        if suppress_location or not act.location:
            return ""
        return f"（{act.location}）"

    def _companions(act: ScheduleActivity) -> str:
        character_names = [
            ref.display_name
            for ref in act.participant_refs
            if ref.actor_kind == "character" and ref.display_name
        ]
        names = character_names or [n for n in act.companion_names if n]
        if not names:
            return ""
        return f"｜一起：{ '、'.join(names) }"

    def _has_visible_companions(act: ScheduleActivity) -> bool:
        return bool(
            act.companion_names
            or any(
                ref.actor_kind == "character" and ref.display_name
                for ref in act.participant_refs
            )
        )

    if current is None:
        lines.append("目前活動：空檔、沒有特定安排")
        lines.append("忙碌程度：低，可以放鬆地回應")
        if just_finished is not None:
            time_range = _format_range(just_finished, local_tz=local_tz)
            lines.append(
                f"剛結束：{time_range} 的「{just_finished.description}」"
                f"{_loc(just_finished)}{_companions(just_finished)}"
                "；現在是轉場空檔，回覆時可以自然地帶到剛做完的事或接下來的安排"
            )
    else:
        time_range = _format_range(current, local_tz=local_tz)
        lines.append(
            f"目前活動：{time_range} 正在「{current.description}」"
            f"{_loc(current)}，類型：{current.category}{_companions(current)}"
        )
        lines.append(f"忙碌程度：{_busy_hint(current.busy_score)}")
        if _has_visible_companions(current):
            lines.append(
                "提示：這個時段不是獨自進行 —— 回覆時可以自然地把同伴帶進來"
                "（例如他/她剛剛說了什麼、現在的氛圍）；切勿主動講起『一個人在做這件事』"
                "或暗示自己正獨處。"
            )
    if upcoming:
        lines.append("接下來：")
        for activity in upcoming:
            time_range = _format_range(activity, local_tz=local_tz)
            lines.append(
                f"- {time_range} {activity.description}{_loc(activity)}"
                f"{_companions(activity)}"
            )
    return lines


def _render_completed_today_block(
    *,
    completed: list[ScheduleActivity],
    just_finished: ScheduleActivity | None = None,
    local_tz: tzinfo,
) -> list[str]:
    just_finished_id = just_finished.id if just_finished is not None else None
    rows = [activity for activity in completed if activity.id != just_finished_id]
    if not rows:
        return []
    lines = [
        "今天稍早已完成（這些是你今天確實做過的事；使用者問今天做了什麼時可自然帶到，請勿照稿念出）：",
    ]
    for activity in rows:
        location = f"（{activity.location}）" if activity.location else ""
        lines.append(
            f"- {_format_range(activity, local_tz=local_tz)} "
            f"{activity.description}{location}",
        )
    return lines


def _render_pending_invites_block(
    *,
    pending: list[ScheduleActivity],
    local_tz: tzinfo,
) -> list[str]:
    if not pending:
        return []
    activity = pending[0]
    location = f"（{activity.location}）" if activity.location else ""
    return [
        "尚未確認的邀請（只是一個想問對方的念頭；對方還沒答應，不要說成已約好）：",
        f"- {_format_range(activity, local_tz=local_tz)} {activity.description}{location}",
        "找機會自然問出口即可；若對方沒有回應，不要追問，也不要把這件事當成共同回憶。",
    ]


def _busy_hint(score: float) -> str:
    """Translate a 0–1 busy score into a reply-tone instruction.

    Thresholds are intentionally coarse — the model treats these as
    soft nudges. The phrases avoid numeric values so the model is less
    tempted to echo them literally in the reply.
    """
    if score >= 0.85:
        return "非常高，手邊的事需要專注，回覆可以簡短、語氣帶點忙碌或抱歉，之後再詳聊"
    if score >= 0.6:
        return "偏高，雖然能回訊息但不太方便長談，回覆保持簡潔即可"
    if score >= 0.35:
        return "中等，可以正常聊天，但不要過度展開冗長內容"
    if score >= 0.15:
        return "偏低，有餘裕好好回應、自然延伸話題"
    return "很低，處於放鬆狀態，可以耐心、溫度充足地回覆"


def _format_range(activity: ScheduleActivity, *, local_tz: tzinfo) -> str:
    start = to_timezone(activity.start_at, local_tz).strftime("%H:%M")
    end = to_timezone(activity.end_at, local_tz).strftime("%H:%M")
    return f"{start}-{end}"


def _operator_timezone(
    operator: OperatorProfile | None,
    fallback: tzinfo,
) -> tzinfo:
    if operator is None:
        return fallback
    try:
        return timezone_for_id(getattr(operator, "timezone_id", None))
    except ValueError:
        return fallback


def _render_tools_block(
    tools: list[PromptToolDescriptor],
    *,
    forced_tool_name: str | None = None,
) -> list[str]:
    """Instruct the model about available tools + the JSON call format.

    The format is deliberately single-call-per-reply — we don't try to
    support multi-call parallel dispatch yet. The model either replies
    in natural language to the user, or emits a fenced JSON block that
    parses to ``{"tool": name, "args": {...}}``. The chat orchestrator
    catches the JSON, runs the tool, and re-prompts with the result
    injected, so the final user-visible reply can reference it.

    When ``forced_tool_name`` is set, the fixed command trigger
    fired — the user explicitly asked for this tool's output. We inject
    a hard directive that overrides the normal "judge whether a tool
    fits" framing: this turn must emit a JSON call to that tool, with
    arguments the model picks from conversation context (so the image
    still reflects the scene being discussed, not the raw command).
    """
    if not tools:
        return []
    lines: list[str] = []
    if forced_tool_name:
        lines.extend(
            [
                f"⚡ 本回合強制工具呼叫：使用者訊息命中了操作者設定的觸發規則，"
                f"**這回合必須呼叫 `{forced_tool_name}` 工具**，禁止純文字回覆。",
                f"- 參數（例如 `generate_image` 的 `positive`）請依當前對話情境與角色處境自行決定，"
                f"不要照抄使用者的觸發指令字面（例如 `/pic`、`幫我畫` 這類命令前綴不要當成畫面內容）。",
                f"- 若使用者觸發指令後方有補充描述（例如「/pic 咖啡廳窗邊的側臉」的「咖啡廳窗邊的側臉」），"
                f"該補充是使用者對畫面的偏好提示，請優先融入 `positive`；"
                f"沒有補充時就完全依據近期對話與當下場景自行構圖。",
                "- 下一輪你會收到工具結果，再以角色身份寫一句自然的收尾台詞即可。",
                "",
            ]
        )
    lines.append(
        "可用工具：這些工具能讓你的回覆更有臨場感，符合下列情境時請主動使用，不要等使用者開口："
    )
    lines.extend([
        "- 生圖（generate_image）：當你此刻要描述『自己正身處一個明確場景、做著一個明確動作或擺出明確姿態／表情』時，"
        "優先用這個工具把畫面傳給對方，再配一句自然、簡短的角色台詞，而不是只用文字長篇描寫。"
        "例如：你正在廚房煮東西、窩在沙發讀書、穿新衣服轉圈、剛洗完頭、趴在桌上偷看使用者——這些都是該直接生圖的時機。",
        "- 當使用者明確或委婉表達想看你的樣子／現在的場景（「現在在幹嘛」「拍給我看」「長什麼樣子」）時，必叫。",
        "- 當你的穿著、所在地、情緒、動作發生明顯變化、值得『給一眼』時，也可以主動生圖。",
        "",
        "避免調用：純聊心情、抽象討論、沒有具體畫面可給、或剛剛已經發過類似的圖還沒推進新畫面。",
        "",
        "工具清單：",
    ])
    for tool in tools:
        lines.append(f"- {tool.name}: {tool.description}")
        try:
            schema_text = json.dumps(
                tool.parameters_schema, ensure_ascii=False, indent=None,
            )
        except (TypeError, ValueError):
            schema_text = "{}"
        lines.append(f"  參數 schema：{schema_text}")
    lines.extend(
        [
            "",
            "呼叫方式：想呼叫工具時，這回合就**只**輸出以下 JSON，不要任何前後文、不要對話、不要旁白、不要表情符號：",
            "```json",
            '{"tool": "工具名稱", "args": {...}}',
            "```",
            "工具執行完後你會收到結果，下一輪再以角色身份自然地回覆使用者。",
            "",
            "嚴禁的錯誤模式（非常重要、常見錯誤）：",
            "❌ 不要把工具呼叫寫成旁白，例如 `*（生成一張圖片：小櫻坐在公園…）*` —— 這是把工具當成文字描述了，工具不會被執行，使用者只會看到一段沒有圖的敘述。",
            "❌ 不要在自然語言回覆中夾 JSON；也不要在 JSON 前後再補「好的我來生圖」這類解釋。",
            "❌ 不要用「*（生成…）*」「*（傳一張自拍…）*」「*（拍一張照片…）*」這類括號旁白代替真正的工具呼叫。",
            "✅ 正確：要給畫面 → 這回合整個輸出就是一段 JSON；要聊天 → 完全不出現 JSON，也不出現 `生成一張圖片` 這種詞。",
            "",
            "若此刻沒有具體畫面或場景可給，就直接用角色的語氣以自然語言回覆，不要輸出任何 JSON，也不要假裝生圖。",
        ]
    )
    return lines


def _render_tool_outcomes_block(outcomes: list[ToolOutcomeMessage]) -> list[str]:
    """Tell the model what the tools it called just returned.

    We keep the payload human-readable rather than echoing the full
    JSON — the model is about to write a reply to the *user*, so it
    needs enough context ("I just generated an image of …") without
    getting distracted by URL structure.
    """
    if not outcomes:
        return []
    lines: list[str] = ["工具回傳結果（已執行，請在回覆中自然地交代結果）："]
    for outcome in outcomes:
        if outcome.ok:
            lines.append(
                f"- {outcome.tool_name} 成功：{outcome.output_text or '（無文字輸出）'}"
            )
            if outcome.attachment_urls:
                lines.append(
                    f"  產出檔案：{len(outcome.attachment_urls)} 個（已附在這則回覆一起送給使用者）"
                )
        else:
            lines.append(
                f"- {outcome.tool_name} 失敗：{outcome.error or '未知錯誤'}"
                "（請以角色語氣向使用者簡短致歉，不要重試）"
            )
    return lines


def _render_story_events_block(events: list[StoryEvent]) -> list[str]:
    """Inject today's story events as the character's own life-colour.

    Unlike world events, these are *first person* — written in the
    character's voice. The prompt frames them as inner reality so the
    model treats them as genuine experiences the character could bring
    up naturally.
    """
    if not events:
        return []
    lines: list[str] = [
        _DIGEST_SOURCE_FRAME,
        "今天你身上發生的小事（第一人稱、是你真的經歷的情緒片段，可自然融入對話）：",
    ]
    for event in events:
        text = event.narrative.strip()
        tone = (event.emotional_tone or "").strip()
        if tone:
            lines.append(f"- ({tone}) {text}")
        else:
            lines.append(f"- {text}")
    lines.append(
        "注意：以上只是情緒與話題素材，**不是你此刻身處的地點或正在做的活動**。"
        "若與上方「行程」段落的當前地點／活動衝突（例：故事說在學校，行程顯示在使用者家），"
        "一律以行程為準；故事內容可作為「剛才」「今天稍早」的回憶帶過，不要假裝自己正在那個場景裡。"
    )
    return lines


_TENSION_HINTS = {
    "setup": "故事才剛起頭",
    "rising": "事情正在往上堆",
    "climax": "重要的時刻要來了",
    "falling": "餘波還在慢慢散",
    "resolution": "告一段落的時候",
}


_SCENE_TYPE_LABELS = {
    "encounter": "日常／相遇",
    "revelation": "頓悟／揭露",
    "conflict": "衝突／拉扯",
    "resolution": "解決／釋懷",
    "interlude": "過場／喘息",
}


def _today_scene_beat(
    arc: "StoryArc | None", today: date_type | None,
) -> "StoryArcBeat | None":
    """Find the beat the model should play today, if any.

    Direction B keeps due beats pending until the interaction actually
    plays them, so this directive should target pending/active beats
    only. Realized beats flow through StoryEvent / memory and must not
    be forced into the next reply again.
    """
    if arc is None or today is None:
        return None
    candidates = [
        b for b in arc.beats
        if b.scheduled_date <= today
        and b.status in {"pending", "active"}
    ]
    if not candidates:
        return None
    # Earliest overdue/today beat wins; stable when the planner emits
    # "morning + afternoon" beats on the same day.
    candidates.sort(key=lambda b: (b.scheduled_date, b.sequence))
    return candidates[0]


def _scene_has_structure(beat: "StoryArcBeat") -> bool:
    """Cheap mirror of ``SceneContext.is_meaningful`` — used here
    instead of constructing a SceneContext just to ask the question."""
    return bool(
        beat.location
        or beat.scene_characters
        or beat.dramatic_question
    )


def _render_today_scene_directive_block(
    *,
    arc: "StoryArc | None",
    today: date_type | None,
) -> list[str]:
    """Strong directive segment for today's scripted scene beat.

    Distinct from ``_render_story_arc_block`` (informational forward
    feed) and ``_render_story_events_block`` (the narrative material
    of today's event): this block tells the model **what scene to
    play right now** — location, who else is there, what tension
    drives the moment. Emits nothing when there's no beat for today
    or when the beat carries no scene structure (older arcs, gacha-
    only days), so a character without scripted scenes sees no extra
    noise in the prompt.
    """
    beat = _today_scene_beat(arc, today)
    if beat is None or not _scene_has_structure(beat):
        return []
    label = _SCENE_TYPE_LABELS.get(beat.scene_type, beat.scene_type)
    header = (
        "【今日場景指引（必演）】" if beat.required
        else "【今日場景指引（可選；可在自然處帶過）】"
    )
    lines: list[str] = [
        header,
        "今天的對話應自然進入下方這場戲（不要逐句念骨架，用角色當下感受演出）：",
        f"- 場景類型：{label}",
    ]
    if beat.location:
        lines.append(f"- 場景地點：{beat.location}")
    if beat.scene_characters:
        lines.append(
            f"- 出場人物（除你之外）：{'、'.join(beat.scene_characters)}"
        )
    if beat.dramatic_question:
        lines.append(f"- 戲劇問題：{beat.dramatic_question}")
    overdue_days = max(0, (today - beat.scheduled_date).days) if today else 0
    if overdue_days:
        lines.append(f"- 已經延後：{overdue_days} 天")
    if beat.play_attempt_count:
        lines.append(f"- 已嘗試帶出：{beat.play_attempt_count} 次")
    if beat.last_play_push_intensity:
        lines.append(f"- 上次推進力道：{beat.last_play_push_intensity}")
    if beat.last_play_attempt_result:
        lines.append(f"- 上次結果：{beat.last_play_attempt_result}")
    # Title gives the LLM a one-phrase anchor; useful when the realized
    # event narrative is in a different angle than the beat title.
    lines.append(f"- 場景標題：{beat.title}")
    lines.append(
        "在合適時機讓這場戲自然發生 —— 場景的氣氛、出場人物的存在、戲劇問題的張力，"
        "至少要在本回合的回覆中**浮現一個**。如果使用者岔題，你可以自然地把話題引回來。"
    )
    return lines


def _render_story_arc_block(
    *,
    arc: StoryArc | None,
    upcoming: list[StoryArcBeat],
    today: date_type | None,
) -> list[str]:
    """Forward-feed arc context: current premise + next 1–2 pending beats.

    Gives the model **anticipation** — it can drop hints like "再兩天
    就要試鏡了" naturally in conversation without the operator having
    to inject it manually. Realized beats are rendered separately by
    ``_render_arc_history_block``; today's beat (if any) is handled by
    ``_render_today_scene_directive_block`` so this block stays purely
    informational.
    """
    if arc is None:
        return []
    lines: list[str] = [
        _DIGEST_SOURCE_FRAME,
        "你正在經歷的一段故事（主軸；對話可以自然呼應、但不要背臺詞）：",
        f"- 主題：{arc.title}",
        f"- 前情：{arc.premise}",
    ]
    if upcoming:
        lines.append("接下來即將發生的節奏：")
        for beat in upcoming:
            hint = _TENSION_HINTS.get(beat.tension, beat.tension)
            delta_label = _format_date_delta(today, beat.scheduled_date)
            # Summary is paragraph-length; trim for prompt economy.
            snippet = beat.summary.strip()
            if len(snippet) > 120:
                snippet = snippet[:117] + "…"
            lines.append(
                f"- {delta_label}：{beat.title}（{hint}）— {snippet}"
            )
    return lines


def _render_arc_history_block(arc: StoryArc | None) -> list[str]:
    if arc is None:
        return []
    beats = arc.realized_history_beats(limit=5)
    if not beats:
        return []
    lines = [
        "這段故事至今你們已經一起經歷過（確實發生過，可自然延續，不要當成未來預告）：",
    ]
    for beat in beats:
        hint = _TENSION_HINTS.get(beat.tension, beat.tension)
        snippet = beat.summary.strip()
        if len(snippet) > 120:
            snippet = snippet[:117] + "…"
        lines.append(f"- 《{beat.title}》{snippet}（{hint}）")
    return lines


def _format_date_delta(today: date_type | None, target: date_type) -> str:
    if today is None:
        return target.isoformat()
    delta = (target - today).days
    if delta == 0:
        return "今天"
    if delta == 1:
        return "明天"
    if delta == 2:
        return "後天"
    if delta > 0:
        return f"再 {delta} 天"
    if delta == -1:
        return "昨天"
    return f"{-delta} 天前"
