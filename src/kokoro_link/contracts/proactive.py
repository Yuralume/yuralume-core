"""Ports for proactive (character-initiated) messaging.

The pipeline is two-layer:

* ``ProactiveGatePort`` — cheap heuristic. Returns fast without touching
  the LLM. Signals: rate-limit, cooldown, user idle window, the
  character's own schedule (don't bother when they're "asleep").
* ``ProactiveDeciderPort`` — expensive. Takes the full character
  context and asks an LLM "do you want to say anything? if yes, what?".

``ProactiveAttemptRepositoryPort`` persists the audit log so operators
can see why the system was quiet or noisy at any point.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone, tzinfo
from typing import Protocol

from kokoro_link.contracts.persona_curiosity import PersonaCuriosityPlan
from kokoro_link.contracts.prompt import PromptToolDescriptor
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.deferred_intent import DeferredIntent
from kokoro_link.domain.entities.operator_address_preference import (
    OperatorAddressPreference,
)
from kokoro_link.domain.entities.proactive_attempt import ProactiveAttempt
from kokoro_link.domain.entities.schedule import DailySchedule, ScheduleActivity
from kokoro_link.domain.entities.story_arc import StoryArc, StoryArcBeat
from kokoro_link.domain.entities.story_event import StoryEvent
from kokoro_link.domain.value_objects.proactive_trigger import ProactiveTrigger
from kokoro_link.domain.value_objects.tool_call import ToolCall


@dataclass(frozen=True, slots=True)
class GateVerdict:
    """Cheap-gate result."""
    passed: bool
    reason: str


@dataclass(frozen=True, slots=True)
class ProactiveContext:
    """Bundle of signals handed to the LLM decider.

    Kept wide on purpose so the decider can weigh character personality,
    current state, in-progress activity, recent top memories and goals
    together. Omit fields (use None / empty) when the adapter has none
    of that data — the prompt template tolerates missing pieces.
    """
    character: Character
    trigger: ProactiveTrigger
    now: datetime
    current_activity: ScheduleActivity | None
    upcoming_activities: list[ScheduleActivity]
    schedule: DailySchedule | None
    idle_minutes: float | None
    """How long since the user last talked to this character on any
    surface. ``None`` means we have no prior conversation at all."""
    sent_today: int
    last_proactive_at: datetime | None
    just_finished_activity: ScheduleActivity | None = None
    """What ended just before ``now`` when the character is in a gap
    (``current_activity is None``). Populated only within the
    service-side freshness window (~3 hours) so a morning meeting stops
    surfacing in an evening push. ``None`` when the current moment is
    covered by an activity, or nothing has ended recently."""
    recent_memories_text: str = ""
    active_goals_text: str = ""
    available_tools: tuple[PromptToolDescriptor, ...] = ()
    """Tools the character is allowed to invoke as part of this
    proactive push (e.g. ``generate_image``). Empty tuple = no tools
    offered — the decider must produce a text-only message."""
    story_events: tuple[StoryEvent, ...] = ()
    """Today's personal story events — things that happened to the
    character. Strongest source of spontaneous conversation hooks ("今天
    做了個奇怪的夢…") because they're in-voice first-person. Empty when
    the gacha pipeline hasn't produced anything today."""
    recent_dialogue_summary: str = ""
    """Pre-condensed blurb of the character's latest chat with the user,
    produced by a ``DialogueSummarizerPort`` before the decider runs.
    Empty string = no context available (no dialogue yet, or summariser
    returned blank). The decider uses this to avoid opening on topics
    the two of them are already mid-conversation about."""
    active_arc: StoryArc | None = None
    """The character's active story arc at the moment of evaluation. Same
    object the chat-path prompt builder sees — lets the decider anchor
    a proactive opener to the current arc premise instead of drifting
    into random "今天天氣" talk. ``None`` when the arc service is not
    wired or the character has no active arc yet."""
    upcoming_beats: tuple[StoryArcBeat, ...] = ()
    """Next 1–2 pending beats (today + near-future) from ``active_arc``.
    Empty when there is no active arc or all beats are realised / skipped."""
    beat_awaiting_player: StoryArcBeat | None = None
    """A due beat whose ``operator_position`` is ``central`` — a scene
    that is *about* the player and therefore cannot be played without
    them.

    Deliberately **not** a slice of ``upcoming_beats``: that tuple is a
    forward feed (``scheduled_date >= today``) and drops a beat the day
    after it comes due, which is exactly when it has been waiting the
    longest. This field instead names the earliest *due* one (scheduled
    on or before today, still pending), so "a scene has been waiting for
    you since Tuesday" stays visible.

    ``None`` — the overwhelmingly common case — means no such beat, and
    both proactive prompts then render exactly as they did before OP3.
    It is a fact for the LLM to weigh alongside every other candidate
    motive: nothing in the pipeline branches on it beyond surfacing it,
    and no gate, cooldown or quota is relaxed because of it."""
    recent_sent_attempts: tuple[ProactiveAttempt, ...] = ()
    """Most-recent SENT proactive messages the character pushed (newest
    first, typically up to ~8 so several days of pushes stay visible).
    Without this the decider re-generates near-identical messages across
    cooldown windows — same character + same state + same story event →
    same LLM output. Surfacing the actual text lets the decider either
    stay silent or pick a completely different angle. Loaded via
    ``list_recent_sent`` (source-filtered) rather than over-fetching the
    audit log and filtering, because GATE_BLOCKED rows (one per tick)
    otherwise bury cross-day SENT history. Empty when the character has
    no history."""
    unanswered_streak: int = 0
    """How many of the most-recent proactive messages went out *after*
    the user last spoke and remain unreplied — i.e. the character has
    pushed this many times in a row with no response. 0 = the latest
    push was already answered, or there is no proactive / conversation
    history. A pure fact for the LLM to weigh: concern, hurt, sulking,
    or deliberately giving space are all valid persona-driven reactions.
    Per the LLM-first rule, behaviour must never branch on this number
    in code — the prompt surfaces it and the model owns the reaction."""
    world_event_seed_title: str = ""
    """One-line headline of an external world event the character has
    been curated against (RSS pipeline). Empty = no seed claimed for
    this round; the decider falls back to inner-life topics. Non-empty
    = the dispatcher already locked this event to the proactive surface
    so feed/drama can't double-use it."""
    world_event_seed_summary: str = ""
    """Multi-line snippet supporting ``world_event_seed_title`` (≤ 800
    chars). The decider weaves it into a natural opener instead of
    parroting the headline."""
    world_event_seed_source: str = ""
    """Source label (e.g. ``BBC World``) so the character can attribute
    the topic ("剛在 X 看到…") rather than claiming personal
    experience. Empty = unknown source."""
    world_event_seed_locale: str = ""
    """BCP-47-ish locale of the RSS source that produced the event.
    This is a fact for the LLM to judge geographic relevance against
    the operator's location; services must not filter by it."""
    operator_location_context: str = ""
    """Prompt-ready coarse operator location fact. Empty when unset.
    Used alongside source locale so the LLM can decide whether a local
    external event matters to this user."""
    calendar_context: str = ""
    """Pre-rendered natural-language block describing today's real-
    world civil calendar (weekday, national holiday, 連假 position,
    nearby holidays, season). Lets the decider write openers that
    track the actual rhythm of the day — "好不容易連假終於到了" on a
    holiday eve, "blue Monday 你也累爆嗎" on a Monday morning.
    Empty = no calendar provider wired; the decider falls back to
    schedule + state cues alone (the legacy behaviour)."""
    weather_context: str = ""
    """Pre-rendered current-weather block (city / condition / 23°C /
    today high 26 low 21). Lets proactive openers naturally reflect
    real conditions ("外面好像在下雨耶 / 早上 21° 蠻冷的"). Empty =
    no weather provider wired or the lookup failed; the decider then
    just ignores the weather angle (legacy behaviour). Mirrors the
    fact also injected to chat / planner / feed so different surfaces
    don't disagree about the same day's weather."""
    upcoming_day_schedules: tuple[DailySchedule, ...] = ()
    """Pre-planned tomorrow + day-after schedules from the rolling
    3-day window. Lets the decider open with hooks like "明天有約
    咖啡耶" / "後天那個會議我有點緊張" based on real future plans
    instead of inventing commitments. Empty tuple = no upcoming days
    pre-planned yet; the decider falls back to "today only" reasoning."""
    operator_persona_lines: tuple[str, ...] = ()
    """Prompt-ready lines describing what this character has learned
    about the operator. Per-character and already thresholded by
    OperatorPersonaService; empty when disabled or unknown."""
    player_persona_note: str = ""
    """What the player *declared* about themselves for this pair — the
    counterpart of ``operator_persona_lines``, which is what the
    character worked out on its own.

    Empty is the norm and leaves the composed prompt byte-identical. The
    dispatcher blanks it whenever the outbound sink could carry someone
    other than the account owner: one compose is fanned out to every
    sink, so the least private sink decides what may be staged."""
    initial_relationship_lines: tuple[str, ...] = ()
    """Prompt-ready user-confirmed initial relationship context for this
    character/operator pair. It may tune address, tone and proactive
    boundaries, but must not be treated as a system memory or proof of
    prior in-app interaction."""
    persona_curiosity_plan: PersonaCuriosityPlan | None = None
    """Optional LLM-first plan for naturally learning more about the
    operator on the proactive surface. This is only a candidate intent
    for the intention judge / decider to weigh; it is not a fixed
    question and does not write persona facts."""
    deferred_intents: tuple[DeferredIntent, ...] = ()
    """Still-active motives that the intention judge previously blocked
    but kept around under TTL (HUMANIZATION_ROADMAP §3.4). The next
    judge call surfaces them as a fact-layer block so the LLM can
    decide "is the timing right now?" instead of forgetting the urge
    after one bad tick. Empty when the feature is off or nothing is
    pending."""
    address_preference: "OperatorAddressPreference | None" = None
    """Observed register / address style (HUMANIZATION_ROADMAP §4.2).
    When present and non-empty, the prompt builder surfaces it as the
    "對方說話風格" fact — and per the 2026-05-21 owner decision this
    takes priority over the §3.6 explicit ``operator_pace_preference``.
    ``None`` = observer hasn't accumulated enough signal yet; the
    fallback path uses pace_preference instead."""
    resolved_character_salutation: str | None = None
    """How the player addresses this character, resolved by the
    bidirectional address resolver (seed.character_address_name >
    observed salutation). Set by the dispatcher so an explicit
    per-character seed name outranks — or surfaces ahead of — any
    observed salutation in the proactive intention prompt. ``None`` when
    nothing real resolves (a bare character-name fallback is suppressed
    so the cold-start prompt stays quiet about an unobserved salutation)."""
    operator_primary_language: str = "zh-TW"
    """BCP 47 tag of the character owner's pinned content language.
    Injected by the decider as a fact line so
    the proactive opener lands in the operator's chosen language without
    drift between chat and proactive surfaces. Defaults to ``zh-TW`` so
    legacy callers that haven't been ported keep their behaviour."""
    local_tz: tzinfo = timezone.utc
    """User timezone for civil-date and visible clock rendering. Instants
    remain UTC; this only affects prompt-facing local times."""


@dataclass(frozen=True, slots=True)
class ProactiveDecision:
    should_send: bool
    reason: str
    message: str | None
    tool_calls: tuple[ToolCall, ...] = field(default_factory=tuple)
    """Optional tool invocations the decider wants to pair with the
    message (e.g. a ``generate_image`` call to attach a fresh selfie
    to a "早安" push). The dispatcher runs these before delivering
    and merges their attachments into the outbound message."""
    prompt_assembled: str | None = None
    """The exact prompt this decision came out of, for the audit trail.

    The decider owns prompt assembly, so it is the only layer that can
    hand the real text back; the dispatcher persists it on the turn
    record when — and only when — the message actually goes out
Skip / blocked
    ticks drop it on the floor: at a ~5-minute cadence they would bury
    the table in prompts nobody will ever read.

    ``None`` = no prompt was assembled (short-circuit paths such as the
    fake provider, or a decider implementation that doesn't prompt at
    all). Implementations are free to leave it unset; the dispatcher
    then records an empty prompt exactly as it did before this field
    existed."""


class ProactiveGatePort(Protocol):
    async def check(
        self,
        *,
        character: Character,
        trigger: ProactiveTrigger,
        now: datetime,
        sent_today: int,
        last_attempt_at: datetime | None,
        idle_minutes: float | None,
        current_activity: ScheduleActivity | None,
        local_tz: tzinfo | None = None,
        cooldown_exempt: bool = False,
    ) -> GateVerdict:
        """``cooldown_exempt`` waives **only** the per-attempt cooldown.

        Set by the dispatcher when a deferred motive's own ``revisit_at``
        alarm has come due: the character agreed to be somewhere at 19:30
        and the 19:22 evaluation must not eat the 19:30 window. Every
        other throttle — idle threshold, daily limit, quiet hours,
        energy — still applies, because none of them is about "we just
        looked at this a moment ago".
        """
        ...


class ProactiveDeciderPort(Protocol):
    async def decide(self, context: ProactiveContext) -> ProactiveDecision: ...


class ProactiveAttemptRepositoryPort(Protocol):
    async def add(self, attempt: ProactiveAttempt) -> None: ...

    async def list_for_character(
        self, character_id: str, *, limit: int = 50,
    ) -> list[ProactiveAttempt]: ...

    async def list_recent_sent(
        self, character_id: str, *, limit: int = 8,
    ) -> list[ProactiveAttempt]:
        """Most-recent actually-SENT proactive messages (newest first).

        Distinct from ``list_for_character``, which returns every
        outcome: the audit log is dominated by GATE_BLOCKED rows (one
        per ~5-min tick), so filtering SENT out of a fixed over-fetch
        silently loses cross-day history once a character has been
        ticked a few hours. Implementations MUST filter ``outcome ==
        SENT`` at the source so a character that pushed days ago still
        surfaces its own words to the decider / intention judge.
        """
        ...

    async def count_sent_today(
        self, character_id: str, *, now: datetime,
    ) -> int: ...

    async def latest_for_character(
        self, character_id: str,
    ) -> ProactiveAttempt | None: ...

    async def latest_passing_gate_for_character(
        self, character_id: str,
    ) -> ProactiveAttempt | None:
        """Most recent attempt that got past the cheap gate.

        This is the anchor for the cooldown: gate-blocked attempts were
        zero-cost (no LLM call) so they shouldn't reset the clock. If
        we used "last attempt of any kind" the gate would re-block the
        next tick and the cooldown would never lapse in practice.
        """
        ...

    async def delete_for_character(self, character_id: str) -> int: ...
