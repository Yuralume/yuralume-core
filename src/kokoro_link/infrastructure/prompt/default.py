"""Default prompt builder.

Renders character profile, current state, recent turns, grouped
long-term memories, the character's aspirations/goals/intent, today's
scheduled activities (current + upcoming), and the latest user message
into a Chinese plain-text prompt. The project is Chinese-first; writing
the scaffolding in Chinese stops the model from echoing English section
headers or narrative tags in its reply (classic prompt-bleed symptom
with Gemma-family models).

This module is now only the *port adapter*: it owns the public
``build()`` signature, the prompt-pack hash bookkeeping, and the
translation of ~50 optional keyword arguments into one frozen
:class:`~kokoro_link.infrastructure.prompt.sections.context.PromptSectionContext`.
What each block says, where it sits, and which blocks blank each other
all live in ``infrastructure.prompt.sections`` — see ``sections/order.py``
for the section table and ``sections/registry.py`` for the resolvers.
"""

from dataclasses import fields, is_dataclass
from datetime import date as date_type, datetime, timezone, tzinfo
from typing import Mapping

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
from kokoro_link.domain.entities.conversation import Conversation, Message
from kokoro_link.domain.entities.feed_post import FeedPost
from kokoro_link.domain.entities.memory_item import MemoryItem
from kokoro_link.domain.entities.proactive_attempt import ProactiveAttempt
from kokoro_link.domain.entities.schedule import (
    DailySchedule,
    ScheduleActivity,
)
from kokoro_link.domain.entities.operator_profile import OperatorProfile
from kokoro_link.domain.entities.story_arc import StoryArc, StoryArcBeat
from kokoro_link.domain.entities.story_event import StoryEvent
from kokoro_link.domain.entities.story_scene_session import StorySceneSession
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.content_flow import (
    CONTENT_TOLERANCE_FRONTIER,
    sanitize_messages_for_tolerance,
)
from kokoro_link.domain.value_objects.presence_frame import PresenceFrame
from kokoro_link.infrastructure.prompt.sections.context import (
    DialogueContext,
    IdentityContext,
    PromptSectionContext,
    RailsContext,
    ScheduleContext,
    StateContext,
    StoryContext,
    TimeContext,
    ToolsContext,
    VisionContext,
)
from kokoro_link.infrastructure.prompt.sections.registry import assemble
from kokoro_link.infrastructure.prompt.sections.schedule import (
    _operator_timezone,
)
from kokoro_link.infrastructure.prompts import get_default_loader

# Compatibility re-exports. These block renderers moved into
# ``infrastructure.prompt.sections`` when ``build()`` was split into a
# context + registry; callers (and a fair number of tests) still reach
# for them on this module, so the names stay bound here.
from kokoro_link.infrastructure.prompt.sections.dialogue import (  # noqa: F401
    _render_diversity_evidence_block,
    _render_presence_frame_block,
)
from kokoro_link.infrastructure.prompt.sections.identity import (  # noqa: F401
    _render_operator_block,
    _render_register_block,
    _render_turn_register_block,
)
from kokoro_link.infrastructure.prompt.sections.schedule import (  # noqa: F401
    _render_calendar_block,
    _render_pending_invites_block,
    _render_upcoming_days_block,
)
from kokoro_link.infrastructure.prompt.sections.state import (  # noqa: F401
    _DIRECTION_GOALS_MAX,
    _memory_time_tag,
    _render_direction_block,
    _render_emotion_events_block,
    _render_self_reflection_block,
)
from kokoro_link.infrastructure.prompt.sections.story import (  # noqa: F401
    _render_story_arc_block,
    _render_story_events_block,
)
from kokoro_link.infrastructure.prompt.sections.text import (  # noqa: F401
    LATEST_USER_MESSAGE_MARKER,
)


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
        character_tool_names: "tuple[str, ...] | None" = None,
        story_events: list[StoryEvent] | None = None,
        story_arc: "StoryArc | None" = None,
        upcoming_arc_beats: "list[StoryArcBeat] | None" = None,
        story_scene: "StorySceneSession | None" = None,
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
        player_persona_note: str | None = None,
        peer_roster_lines: "list[str] | None" = None,
        initial_relationship_lines: "list[str] | None" = None,
        persona_curiosity_plan: PersonaCuriosityPlan | None = None,
        calendar_context: str = "",
        weather_context: str = "",
        world_event_context: "tuple[str, ...] | None" = None,
        world_event_recall: "tuple[str, ...] | None" = None,
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
        stage_nudge: bool = False,
    ) -> str:
        """Render the chat prompt.

        Two steps and nothing else: snapshot the turn into a frozen
        :class:`PromptSectionContext`, then let the section registry
        render and concatenate it. Every block's content, order and
        mutual exclusion lives in ``infrastructure.prompt.sections``.
        """
        self.last_prompt_pack_hash = get_default_loader().prompt_pack_hash(
            prompt_pack_hash_snapshot(
                self._humanization_settings,
                self._prompt_quality_settings,
            ),
        )
        return assemble(
            self._build_context(
                character=character,
                conversation=conversation,
                recent_messages=recent_messages,
                memories=memories,
                pending_state=pending_state,
                latest_user_message=latest_user_message,
                active_goals=active_goals,
                current_activity=current_activity,
                upcoming_activities=upcoming_activities,
                just_finished_activity=just_finished_activity,
                completed_today_activities=completed_today_activities,
                pending_invite_activities=pending_invite_activities,
                now=now,
                idle_minutes=idle_minutes,
                available_tools=available_tools,
                tool_outcomes=tool_outcomes,
                forced_tool_name=forced_tool_name,
                character_tool_names=character_tool_names,
                story_events=story_events,
                story_arc=story_arc,
                upcoming_arc_beats=upcoming_arc_beats,
                story_scene=story_scene,
                today_local=today_local,
                older_dialogue_summary=older_dialogue_summary,
                vision_markers=vision_markers,
                image_recognition_context=image_recognition_context,
                recent_proactive_messages=recent_proactive_messages,
                recent_feed_posts=recent_feed_posts,
                self_repetition_hint=self_repetition_hint,
                phrase_habit_lines=phrase_habit_lines,
                presence_frame=presence_frame,
                operator=operator,
                operator_persona_lines=operator_persona_lines,
                player_persona_note=player_persona_note,
                peer_roster_lines=peer_roster_lines,
                initial_relationship_lines=initial_relationship_lines,
                persona_curiosity_plan=persona_curiosity_plan,
                calendar_context=calendar_context,
                weather_context=weather_context,
                world_event_context=world_event_context,
                world_event_recall=world_event_recall,
                upcoming_day_schedules=upcoming_day_schedules,
                emotion_events=emotion_events,
                self_reflections=self_reflections,
                address_preference=address_preference,
                experiment_overlay=experiment_overlay,
                content_tolerance=content_tolerance,
                material_digest=material_digest,
                turn_register_profile=turn_register_profile,
                reply_diversity_evidence=reply_diversity_evidence,
                retry_directive=retry_directive,
                resolved_player_address=resolved_player_address,
                resolved_character_address=resolved_character_address,
                address_change_lines=address_change_lines,
                stage_nudge=stage_nudge,
            )
        )

    def _build_context(
        self,
        *,
        character: Character,
        conversation: Conversation,
        recent_messages: list[Message],
        memories: list[MemoryItem],
        pending_state: CharacterState,
        latest_user_message: str,
        active_goals: list[CharacterGoal] | None,
        current_activity: ScheduleActivity | None,
        upcoming_activities: list[ScheduleActivity] | None,
        just_finished_activity: ScheduleActivity | None,
        completed_today_activities: list[ScheduleActivity] | None,
        pending_invite_activities: list[ScheduleActivity] | None,
        now: datetime | None,
        idle_minutes: float | None,
        available_tools: list[PromptToolDescriptor] | None,
        tool_outcomes: list[ToolOutcomeMessage] | None,
        forced_tool_name: str | None,
        character_tool_names: "tuple[str, ...] | None",
        story_events: list[StoryEvent] | None,
        story_arc: "StoryArc | None",
        upcoming_arc_beats: "list[StoryArcBeat] | None",
        story_scene: "StorySceneSession | None",
        today_local: "date_type | None",
        older_dialogue_summary: str | None,
        vision_markers: "Mapping[int, list[int]] | None",
        image_recognition_context: str,
        recent_proactive_messages: "tuple[ProactiveAttempt, ...] | None",
        recent_feed_posts: "tuple[FeedPost, ...] | None",
        self_repetition_hint: str | None,
        phrase_habit_lines: list[str] | None,
        presence_frame: "PresenceFrame | None",
        operator: "OperatorProfile | None",
        operator_persona_lines: "list[str] | None",
        player_persona_note: str | None,
        peer_roster_lines: "list[str] | None",
        initial_relationship_lines: "list[str] | None",
        persona_curiosity_plan: PersonaCuriosityPlan | None,
        calendar_context: str,
        weather_context: str,
        world_event_context: "tuple[str, ...] | None",
        world_event_recall: "tuple[str, ...] | None",
        upcoming_day_schedules: "list[DailySchedule] | None",
        emotion_events: "list | None",
        self_reflections: "list | None",
        address_preference,  # noqa: ANN001 - optional, type checked at use
        experiment_overlay: "dict[str, str] | None",
        content_tolerance: str,
        material_digest: PromptMaterialDigest | None,
        turn_register_profile: RegisterProfile | None,
        reply_diversity_evidence: ReplyDiversityEvidence | None,
        retry_directive: str | None,
        resolved_player_address: "ResolvedAddress | None",
        resolved_character_address: "ResolvedAddress | None",
        address_change_lines: "list[str] | None",
        stage_nudge: bool,
    ) -> PromptSectionContext:
        """Freeze this turn into the snapshot every section reads.

        Everything derived is derived exactly once here — the reference
        clock, the tolerance-sanitised transcript, the operator timezone,
        the defaulted presence frame — so no two sections can disagree
        about what this turn is.
        """
        ref_now = ensure_utc(now) if now is not None else (
            self._clock.now() if self._clock is not None else None
        )
        return PromptSectionContext(
            time=TimeContext(
                now=ref_now,
                today_local=today_local,
                idle_minutes=idle_minutes,
                local_tz=_operator_timezone(operator, self._local_tz),
            ),
            identity=IdentityContext(
                character=character,
                operator=operator,
                operator_persona_lines=tuple(operator_persona_lines or ()),
                player_persona_note=player_persona_note,
                peer_roster_lines=tuple(peer_roster_lines or ()),
                initial_relationship_lines=tuple(
                    initial_relationship_lines or ()
                ),
                address_change_lines=tuple(address_change_lines or ()),
                address_preference=address_preference,
                resolved_player_address=resolved_player_address,
                resolved_character_address=resolved_character_address,
            ),
            state=StateContext(
                pending_state=pending_state,
                memories=tuple(memories),
                active_goals=tuple(active_goals or ()),
                emotion_events=tuple(emotion_events or ()),
                self_reflections=tuple(self_reflections or ()),
            ),
            schedule=ScheduleContext(
                current_activity=current_activity,
                upcoming_activities=tuple(upcoming_activities or ()),
                just_finished_activity=just_finished_activity,
                completed_today_activities=tuple(
                    completed_today_activities or ()
                ),
                pending_invite_activities=tuple(
                    pending_invite_activities or ()
                ),
                upcoming_day_schedules=tuple(upcoming_day_schedules or ()),
                calendar_context=calendar_context,
                weather_context=weather_context,
                world_event_context=tuple(world_event_context or ()),
                world_event_recall=tuple(world_event_recall or ()),
            ),
            story=StoryContext(
                story_events=tuple(story_events or ()),
                story_arc=story_arc,
                upcoming_arc_beats=tuple(upcoming_arc_beats or ()),
                story_scene=story_scene,
            ),
            dialogue=DialogueContext(
                conversation=conversation,
                recent_messages=tuple(
                    sanitize_messages_for_tolerance(
                        recent_messages,
                        content_tolerance=content_tolerance,
                    )
                ),
                latest_user_message=latest_user_message,
                older_dialogue_summary=older_dialogue_summary,
                recent_proactive_messages=tuple(
                    recent_proactive_messages or ()
                ),
                recent_feed_posts=tuple(recent_feed_posts or ()),
                self_repetition_hint=self_repetition_hint,
                phrase_habit_lines=tuple(phrase_habit_lines or ()),
                turn_register_profile=turn_register_profile,
                reply_diversity_evidence=reply_diversity_evidence,
                persona_curiosity_plan=persona_curiosity_plan,
                material_digest=material_digest,
                retry_directive=retry_directive,
                presence_frame=presence_frame or PresenceFrame.web_stage(),
                stage_nudge=stage_nudge,
            ),
            tools=ToolsContext(
                available_tools=tuple(available_tools or ()),
                tool_outcomes=tuple(tool_outcomes or ()),
                forced_tool_name=forced_tool_name,
                # Left as ``None`` when the caller did not declare one —
                # NOT coerced to ``()``. ``()`` is the positive claim
                # "this character can call nothing", and a caller that
                # never mentioned tools has made no claim at all.
                character_tool_names=(
                    None if character_tool_names is None
                    else tuple(character_tool_names)
                ),
            ),
            vision=VisionContext(
                markers=vision_markers or {},
                image_recognition_context=image_recognition_context,
            ),
            rails=RailsContext(
                experiment_overlay=experiment_overlay or {},
                content_tolerance=content_tolerance,
                body_state_enabled=self._humanization_enabled(
                    "body_state_enabled",
                ),
                subjective_time_enabled=self._humanization_enabled(
                    "subjective_time_enabled",
                ),
                address_preference_enabled=self._humanization_enabled(
                    "address_preference_enabled",
                ),
            ),
        )


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
