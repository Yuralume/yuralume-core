"""Typed context handed to every prompt section.

``DefaultPromptContextBuilder.build`` used to carry ~50 optional keyword
arguments straight into a 250-line hand-written ``"\\n".join`` — every
section reached into that flat namespace, so nothing could tell which
inputs a given block actually reads. :class:`PromptSectionContext` is
that namespace made explicit: one frozen snapshot, grouped by domain,
built once per turn and shared by every section.

Two rules keep it honest:

* **Frozen and pre-resolved.** Every derived value the old ``build()``
  computed inline before the join (the reference clock, the tolerance-
  sanitised transcript, the operator timezone, the resolved presence
  frame) is resolved once *here*, so two sections can never disagree
  about what "now" or "this turn's messages" mean.
* **Snapshot, not policy.** :class:`RailsContext` carries the raw
  humanization flags and the experiment overlay; deciding what they
  suppress belongs to the resolvers in ``registry.py``, not to the
  context object.
"""

from dataclasses import dataclass
from datetime import date as date_type, datetime, tzinfo
from typing import Mapping

from kokoro_link.contracts.persona_curiosity import PersonaCuriosityPlan
from kokoro_link.contracts.prompt import PromptToolDescriptor, ToolOutcomeMessage
from kokoro_link.contracts.prompt_material_digest import PromptMaterialDigest
from kokoro_link.contracts.register_profile import RegisterProfile
from kokoro_link.contracts.reply_quality import ReplyDiversityEvidence
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.character_goal import CharacterGoal
from kokoro_link.domain.entities.conversation import Conversation, Message
from kokoro_link.domain.entities.feed_post import FeedPost
from kokoro_link.domain.entities.memory_item import MemoryItem
from kokoro_link.domain.entities.operator_profile import OperatorProfile
from kokoro_link.domain.entities.proactive_attempt import ProactiveAttempt
from kokoro_link.domain.entities.schedule import DailySchedule, ScheduleActivity
from kokoro_link.domain.entities.story_arc import StoryArc, StoryArcBeat
from kokoro_link.domain.entities.story_event import StoryEvent
from kokoro_link.domain.entities.story_scene_session import StorySceneSession
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.presence_frame import PresenceFrame


@dataclass(frozen=True, slots=True)
class TimeContext:
    """The turn's single reference clock and calendar.

    ``now`` is already normalised to UTC (or resolved from the injected
    ``ClockPort``) by the builder — sections must never call
    ``datetime.now`` themselves.
    """

    now: datetime | None
    today_local: date_type | None
    idle_minutes: float | None
    local_tz: tzinfo


@dataclass(frozen=True, slots=True)
class IdentityContext:
    """Who the two parties are: the character sheet plus everything the
    upstream services worked out about the player."""

    character: Character
    operator: OperatorProfile | None
    operator_persona_lines: tuple[str, ...]
    player_persona_note: str | None
    peer_roster_lines: tuple[str, ...]
    initial_relationship_lines: tuple[str, ...]
    address_change_lines: tuple[str, ...]
    address_preference: object | None
    resolved_player_address: object | None
    resolved_character_address: object | None


@dataclass(frozen=True, slots=True)
class StateContext:
    """The character's inner weather and its long-term recall."""

    pending_state: CharacterState
    memories: tuple[MemoryItem, ...]
    active_goals: tuple[CharacterGoal, ...]
    emotion_events: tuple[object, ...]
    self_reflections: tuple[object, ...]


@dataclass(frozen=True, slots=True)
class ScheduleContext:
    """Today's logistics: activities, weather, calendar and world events."""

    current_activity: ScheduleActivity | None
    upcoming_activities: tuple[ScheduleActivity, ...]
    just_finished_activity: ScheduleActivity | None
    completed_today_activities: tuple[ScheduleActivity, ...]
    pending_invite_activities: tuple[ScheduleActivity, ...]
    upcoming_day_schedules: tuple[DailySchedule, ...]
    calendar_context: str
    weather_context: str
    world_event_context: tuple[str, ...]
    world_event_recall: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class StoryContext:
    """Narrative material: past events, the arc, and any live 起幕 scene."""

    story_events: tuple[StoryEvent, ...]
    story_arc: StoryArc | None
    upcoming_arc_beats: tuple[StoryArcBeat, ...]
    story_scene: StorySceneSession | None


@dataclass(frozen=True, slots=True)
class DialogueContext:
    """This turn's conversation surface.

    ``recent_messages`` is the *sanitised* transcript (already filtered
    through the content-tolerance frontier) and ``presence_frame`` is
    already defaulted — sections never re-derive either.
    """

    conversation: Conversation
    recent_messages: tuple[Message, ...]
    latest_user_message: str
    older_dialogue_summary: str | None
    recent_proactive_messages: tuple[ProactiveAttempt, ...]
    recent_feed_posts: tuple[FeedPost, ...]
    self_repetition_hint: str | None
    phrase_habit_lines: tuple[str, ...]
    turn_register_profile: RegisterProfile | None
    reply_diversity_evidence: ReplyDiversityEvidence | None
    persona_curiosity_plan: PersonaCuriosityPlan | None
    material_digest: PromptMaterialDigest | None
    retry_directive: str | None
    presence_frame: PresenceFrame
    stage_nudge: bool


@dataclass(frozen=True, slots=True)
class ToolsContext:
    """What the character may call this turn, and what it already got back."""

    available_tools: tuple[PromptToolDescriptor, ...]
    """Offered on *this hop*. Emptied on the final hop of a tool turn on
    purpose, so it answers "may the model emit a call right now", never
    "what can this character do"."""
    tool_outcomes: tuple[ToolOutcomeMessage, ...]
    forced_tool_name: str | None
    character_tool_names: tuple[str, ...] | None = None
    """Every tool this character can actually invoke this turn, hop
    suppression and all (HV2).

    Tri-state on purpose: ``None`` = the caller did not say, and a section
    reasoning about a *missing* capability must stay silent rather than
    assert an absence it cannot see. An empty tuple is the positive claim
    "this character can call nothing" — which is what a deployment with no
    orchestrator wired, or a character with an empty ``allowed_tools``,
    genuinely is."""


@dataclass(frozen=True, slots=True)
class VisionContext:
    """The cross-turn image inventory.

    ``markers`` maps a turn index to the 1-based ``[圖 N]`` tags that
    belong to it; index ``len(recent_messages)`` is this turn's user
    message. The ordering here must stay identical to the ordering of
    ``image_urls`` sent to the model — the placeholders are positional.
    """

    markers: Mapping[int, list[int]]
    image_recognition_context: str


@dataclass(frozen=True, slots=True)
class RailsContext:
    """Feature flags and the experiment overlay, as a raw snapshot.

    Resolved once per turn so two sections toggled by the same
    experiment can never read different variants mid-build. What each
    flag *suppresses* is decided by the resolvers, not here.
    """

    experiment_overlay: Mapping[str, str]
    content_tolerance: str
    body_state_enabled: bool
    subjective_time_enabled: bool
    address_preference_enabled: bool

    @property
    def include_catchup_hint(self) -> bool:
        """Whether the timing block carries the subjective-time catch-up
        hint (HUMANIZATION_ROADMAP §4.6 overlay key ``subjective_time``).

        Unlike ``body_state`` / ``self_reflection``, this variant does not
        blank a whole section — it narrows one block's content — so it is
        a context read rather than a resolver.
        """
        return (
            self.subjective_time_enabled
            and self.experiment_overlay.get("subjective_time") != "off"
        )


@dataclass(frozen=True, slots=True)
class PromptSectionContext:
    """Everything a chat prompt section is allowed to read."""

    time: TimeContext
    identity: IdentityContext
    state: StateContext
    schedule: ScheduleContext
    story: StoryContext
    dialogue: DialogueContext
    tools: ToolsContext
    vision: VisionContext
    rails: RailsContext
