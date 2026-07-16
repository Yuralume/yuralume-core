from dataclasses import dataclass
from datetime import date as date_type, datetime
from typing import Any, Mapping, Protocol

from kokoro_link.contracts.persona_curiosity import PersonaCuriosityPlan
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
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.content_flow import CONTENT_TOLERANCE_FRONTIER
from kokoro_link.domain.value_objects.presence_frame import PresenceFrame


@dataclass(frozen=True, slots=True)
class PromptToolDescriptor:
    """What the prompt builder needs to describe an available tool.

    Decoupled from ``ToolPort`` so the prompt module doesn't pull in
    the tool-contract transitively (would create a cycle between
    contracts/ and infrastructure/tools via type hints).
    """

    name: str
    description: str
    parameters_schema: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ToolOutcomeMessage:
    """A completed tool call, injected into the next model turn so the
    model can incorporate the result into its reply.

    ``ok=False`` means the tool failed; the model is still asked to
    reply (usually apologising) rather than swallowing the failure.
    """

    tool_name: str
    ok: bool
    output_text: str
    attachment_urls: tuple[str, ...] = ()
    error: str | None = None


class PromptContextBuilderPort(Protocol):
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
        now: datetime | None = None,
        idle_minutes: float | None = None,
        available_tools: list[PromptToolDescriptor] | None = None,
        tool_outcomes: list[ToolOutcomeMessage] | None = None,
        forced_tool_name: str | None = None,
        story_events: list[StoryEvent] | None = None,
        story_arc: StoryArc | None = None,
        upcoming_arc_beats: list[StoryArcBeat] | None = None,
        today_local: date_type | None = None,
        older_dialogue_summary: str | None = None,
        vision_markers: Mapping[int, list[int]] | None = None,
        image_recognition_context: str = "",
        recent_proactive_messages: tuple[ProactiveAttempt, ...] | None = None,
        recent_feed_posts: tuple[FeedPost, ...] | None = None,
        self_repetition_hint: str | None = None,
        phrase_habit_lines: list[str] | None = None,
        presence_frame: PresenceFrame | None = None,
        operator: OperatorProfile | None = None,
        operator_persona_lines: list[str] | None = None,
        peer_roster_lines: list[str] | None = None,
        initial_relationship_lines: list[str] | None = None,
        persona_curiosity_plan: PersonaCuriosityPlan | None = None,
        calendar_context: str = "",
        weather_context: str = "",
        world_event_context: tuple[str, ...] | None = None,
        upcoming_day_schedules: list[DailySchedule] | None = None,
        content_tolerance: str = CONTENT_TOLERANCE_FRONTIER,
    ) -> str:
        """Build prompt context for model generation.

        ``now`` is the wall clock at the moment this reply is being
        generated; the builder renders it as local time so the model
        can reason about "it's late", "just after lunch", etc.
        ``idle_minutes`` is the gap since the user last sent a message
        — lets the character acknowledge "good morning" / "been a
        while" naturally without the caller having to craft prose.
        Both default to ``None`` which the builder treats as "no such
        signal available" rather than fabricating zeros.

        ``calendar_context`` is the same pre-rendered natural-language
        block the schedule planner sees, describing today's real-world
        civil calendar (weekday, national holiday, 連假 position,
        nearby holidays, season). Empty string = no calendar provider
        wired; the builder renders nothing for the calendar section.

        ``weather_context`` is the same pre-rendered current-weather
        block used by schedule/proactive/feed.

        ``world_event_context`` is an optional read-only RSS fact block
        for chat. It may include source locale and operator location;
        the builder renders it as facts and the LLM decides relevance.

        ``image_recognition_context`` is the multimodal recognition
        summary for a text-only main model (empty when the model sees
        images natively or no images exist). The builder renders it in
        the prompt body next to the ``[圖 N]`` legend — callers must
        NOT append it after the assembled prompt, where it lands behind
        the instruction footer and reads as part of the user's message.

        ``upcoming_day_schedules`` is the next 1–2 already-planned days
        after today (rolling-window pre-plan). The builder renders them
        compactly (tomorrow more detailed than day-after) so the chat
        model can answer "明天有空嗎 / 後天要幹嘛" against the same
        schedule the planner will actually produce on those days,
        instead of fabricating commitments that won't match. Empty
        list (or omitted) = no pre-planned upcoming days yet; the
        builder appends a "remote future is vague" guard so the model
        admits it hasn't decided.
        """
