"""Post-turn processor port.

After a conversation turn completes, the post-turn processor runs a
single LLM call (or no-op) to extract long-term memories **and**
suggest character state updates. Combining both tasks in one call
avoids doubling inference cost.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Protocol

from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.conversation import Message
from kokoro_link.domain.entities.memory_item import MemoryItem
from kokoro_link.domain.entities.operator_profile import OperatorProfile
from kokoro_link.domain.entities.schedule import DailySchedule
from kokoro_link.domain.entities.story_arc import StoryArc


@dataclass(frozen=True, slots=True)
class StateSuggestion:
    """LLM-recommended state changes after a conversation turn."""

    emotion: str | None = None
    affection_delta: int = 0
    fatigue_delta: int = 0
    trust_delta: int = 0
    energy_delta: int = 0
    current_intent: str | None = None
    """Revised short-term intent for the character (next few turns)."""


@dataclass(frozen=True, slots=True)
class ScheduleAdjustment:
    """A single mutation to a character's daily schedule.

    Driven by conversation context — e.g. the user saying "今天下午
    的會議取消了" yields a ``remove`` targeting the matching activity.

    Fields are intentionally loose so the LLM can emit the action it's
    most confident about. Validation happens in
    ``ScheduleService.apply_adjustments`` — unknown actions, missing
    ids, or malformed times drop the adjustment without error.
    """

    action: str  # "add" | "remove" | "modify"
    activity_id: str | None = None
    start: str | None = None  # "HH:MM" in the character's local timezone
    end: str | None = None
    description: str | None = None
    category: str | None = None
    location: str | None = None
    busy_score: float | None = None
    operator_involvement: str | None = None
    """Structured operator participation state for shared schedule items."""
    operator_display_name: str | None = None
    reason: str | None = None  # optional natural-language justification
    target_date_iso: str | None = None
    """ISO date (``YYYY-MM-DD``) the adjustment applies to. ``None`` =
    today (legacy behaviour); a future date lands the activity on
    that day's schedule, lazy-creating it as a ``is_planned=False``
    seed row so the next ``ensure_schedule`` pass folds it into a
    full day plan. Used to capture conversation-extracted future
    commitments like "明天 7 點看電影" so they show up when the
    actual day arrives rather than being forgotten between turns."""


@dataclass(frozen=True, slots=True)
class ArcAdjustmentSignal:
    """Post-turn signal to mutate the active story arc.

    Actions:

    - ``advance_beat`` / ``delay_beat``: shift a pending beat's
      scheduled date by ``days``. Negative ``days`` for advance is
      also accepted; service clamps to a sane range.
    - ``modify_beat``: rewrite a pending beat's ``title`` / ``summary`` /
      ``tension``. Realized beats are never rewritten (history is
      preserved).
    - ``insert_beat``: append a new beat at ``scheduled_date`` with the
      given fields.
    - ``mark_realized``: flip a pending beat to realized because it
      actually surfaced mid-conversation; may include ``narrative`` so
      the event recorder can persist what happened.
    - ``skip_beat``: mark a pending beat skipped when the scene should
      fade out instead of being forced.

    Unknown / malformed entries are silently dropped by the service,
    mirroring ``ScheduleAdjustment`` tolerance.
    """

    action: str
    beat_id: str | None = None
    days: int | None = None
    scheduled_date: date | None = None
    title: str | None = None
    summary: str | None = None
    tension: str | None = None
    reason: str | None = None
    narrative: str | None = None


@dataclass(frozen=True, slots=True)
class MessagePromise:
    """A character promise to message the user at a specific future time.

    Extracted from conversation by the post-turn LLM when the user
    explicitly asked the character to text/wake/remind them at some
    moment (例: "明天 10 點叫我起床" / "中午記得提醒我吃飯").
    Routes through :class:`PendingFollowUp` (``kind=scheduled_promise``)
    so the proactive dispatcher actually sends the message — bypassing
    quiet_hours / daily_limit / cooldown gates because this is a
    promise fulfilment, not unsolicited push.

    Distinct from :class:`ScheduleAdjustment(action="add")` even though
    the same chat turn often generates both: the schedule-adjustment is
    "the character's day now contains a 10am wake-up activity", while
    the message-promise is "the character will actually send an
    outbound at 10am". Without the promise the schedule activity sits
    in the day silently and never reaches the user (proactive subsystem
    may be off, daily limit hit, etc).
    """

    scheduled_for_iso: str
    """Full ISO-8601 timestamp (``YYYY-MM-DDTHH:MM`` or with seconds /
    timezone) of the promised moment. Service-side parser tolerates
    naive datetimes (assumes character's local timezone) and rejects
    malformed input. Past-dated entries are silently dropped — the
    LLM occasionally mis-types ``2025`` for ``2026``."""
    intent: str
    """Natural-language description of what the character promised to
    do at ``scheduled_for_iso`` (例: "叫使用者起床" / "提醒使用者吃
    午餐"). The composer reads this and writes the actual message
    interpretation through the character's persona."""
    source_text: str = ""
    """Original user-side wording that produced the promise (例: "明天
    10 點叫我起床嘛"). Optional — empty when the post-turn LLM can't
    distil a clean quote. Carried into the composer's prompt as
    "對方當初的原話"."""


@dataclass(frozen=True, slots=True)
class PeerMeetIntent:
    """A chat-extracted agreement for this character to meet a known peer."""

    peer_character_id: str
    """Known peer character id from the injected peer roster."""
    desired_after_iso: str
    """ISO datetime for the earliest acceptable encounter time.

    Date-only user requests are normalized by the processor to midnight
    local civil time; the planner then finds the first valid low-busy slot
    on or after that point.
    """
    topic: str
    """Natural-language topic/reason to carry into encounter trigger_reason."""
    peer_name: str = ""
    source_text: str = ""


@dataclass(frozen=True, slots=True)
class AddressChangeSignal:
    """A chat-observed change in how the operator and character address
    each other (HUMANIZATION / ADDRESS_RESOLVER_PLAN).

    When the user says something like 「今天開始叫我森森」 (direction
    ``player``: the *character* should address the *user* as 森森) or
    「我以後叫你小美」 (direction ``character``: the *user* will address
    the *character* as 小美), the post-turn LLM emits this typed signal
    instead of a free-text memory. ``ChatService`` routes it through the
    address-change governance (seed update + per-direction rename log +
    persona name reconcile). Routing it as a *typed directional* event —
    rather than a first-person memory — avoids the direction inversion
    that happens when 「叫我」 is rewritten from the character's POV, and
    keeps a private naming preference out of the public LumeGram feed.
    """

    direction: str
    """``"player"`` = character should call the USER ``new_value``;
    ``"character"`` = user will call the CHARACTER ``new_value``."""
    new_value: str
    """The address term the speaker is asking for."""
    subject: str = ""
    """Who ``new_value`` names, classified by the model
    (``operator_self`` / ``other_person`` / ``character`` / ``unclear``).
    A ``player``-direction change writes the operator's *own* identity
    name, so it is only accepted when ``subject == "operator_self"`` —
    this keeps a mis-read like 「叫小美過來」 (naming a peer) from landing
    as the operator's name, mirroring the persona extractor's subject
    discipline on the sibling write path."""
    old_value: str = ""
    """Prior term if the user referenced one; empty otherwise."""
    source_text: str = ""
    """Verbatim user wording that triggered the change (for audit)."""


@dataclass(frozen=True, slots=True)
class EmotionEventCandidate:
    """LLM-emitted EmotionEvent candidate — Phase 3 of the emotion-event
    rewrite (HUMANIZATION_ROADMAP §2.2).

    Richer than the deltas-only :class:`StateSuggestion` mirror: carries
    valence / arousal / intensity / evidence quote / half-life so the
    aggregator can rank and decay this event independently. ``ChatService``
    persists candidates as :class:`EmotionEvent` rows.

    Empty / absent list signals "fall back to ``state_suggestion`` mirror"
    so back-compat with the older prompt schema is preserved during
    rollout.
    """

    emotion_label: str = ""
    evidence_quote: str = ""
    valence: float = 0.0
    """-1.0 (strongly negative) .. +1.0 (strongly positive)."""
    arousal: float = 0.0
    """0.0 (calm) .. 1.0 (highly activated). Joy and anger both score high."""
    intensity: float = 0.5
    """0.0 .. 1.0 — how big a deal this moment is for the character."""
    affection_delta: int = 0
    fatigue_delta: int = 0
    trust_delta: int = 0
    energy_delta: int = 0
    decay_half_life_minutes: int = 240
    """How fast this event fades. 240 = 4h (default rest-recovery scale).
    Use longer (≥720) for relationship milestones; shorter (60–120) for
    transient moods like surprise."""


@dataclass(frozen=True, slots=True)
class PostTurnResult:
    """Combined output of memory extraction and state refinement."""

    memories: list[MemoryItem] = field(default_factory=list)
    state_suggestion: StateSuggestion | None = None
    schedule_adjustments: list[ScheduleAdjustment] = field(default_factory=list)
    arc_adjustments: list[ArcAdjustmentSignal] = field(default_factory=list)
    message_promises: list[MessagePromise] = field(default_factory=list)
    """Future-time promises the character made to message the user.
    Routed through :class:`PendingFollowUp` to bypass proactive gates.
    Empty list (default) keeps every legacy code path identical."""
    peer_meet_intents: list[PeerMeetIntent] = field(default_factory=list)
    """Future character-to-character meeting agreements.

    Empty list means no explicit user/character agreement was detected.
    """
    emotion_events: list[EmotionEventCandidate] = field(default_factory=list)
    """Optional richer event-sourcing variant of ``state_suggestion``.
    When non-empty, :class:`ChatService` persists each as an
    :class:`EmotionEvent` row (skipping the lite ``state_suggestion``
    mirror). Empty list = legacy ``state_suggestion`` mirror path.
    1-3 events typical per turn; the LLM emits at most a handful even
    when the conversation is emotionally charged."""
    address_changes: list[AddressChangeSignal] = field(default_factory=list)
    """Chat-observed address/naming changes (「叫我森森」). Routed through
    the address-change governance instead of being written as a memory,
    so the direction is never inverted and the change does not leak into
    the public feed. Empty list = no address change this turn (default)."""


class PostTurnProcessorPort(Protocol):
    async def process(
        self,
        *,
        character: Character,
        conversation_id: str,
        user_message: str,
        assistant_message: str,
        recent_messages: list[Message] | None = None,
        active_schedule: DailySchedule | None = None,
        active_arc: StoryArc | None = None,
        operator: OperatorProfile | None = None,
        content_mode: str = "normal",
        now: datetime | None = None,
        peer_context_lines: list[str] | None = None,
    ) -> PostTurnResult:
        """Extract memories, suggest state updates, propose schedule
        adjustments, and — when an arc is active — emit optional arc
        adjustments based on conversation content.

        ``recent_messages`` is prior dialogue (excluding the current
        turn). A single turn often isn't enough to understand context
        — earlier turns may set up what the user just said. Passing
        them in lets the extractor reason over multi-turn situations
        rather than isolated pairs.
        """
