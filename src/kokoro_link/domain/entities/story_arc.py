"""Multi-week story arc domain entities.

Sits between ``CharacterGoal`` (user-typed todos, open-ended) and
``StoryEvent`` (atomic, one-day narrative) as the **narrative spine**
driving a character's world for weeks at a time. A ``StoryArc`` has:

- A ``title`` / ``premise`` / ``theme`` describing the overall plot
- A series of ordered ``StoryArcBeat``s scheduled on specific dates
- Beats progress through tension levels (setup → rising → climax →
  falling → resolution) so the prompt builder can forward-feed
  anticipation ("再 3 天她就要上台了")

**Why a separate entity from StoryEvent**: events are the performed
materialization; arcs are the plan. A beat, when its scheduled date
arrives, is surfaced as a scene directive. It becomes a ``StoryEvent``
only after a chat/proactive turn actually plays the beat and the
post-turn processor marks it realized. Beats not yet due stay pending
and can be moved / rewritten by either the operator (via UI) or the
chat (via ``arc_adjustments`` in the post-turn processor).

**Immutability**: both entities are frozen dataclasses — mutation
goes through ``with_*`` helpers that return new instances, matching
the rest of the codebase.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from uuid import uuid4

# --- Tension levels ---------------------------------------------------
# Free strings rather than an enum so the LLM planner can introduce
# intermediate shades ("rising_2") if it wants, and the prompt builder
# falls back to a generic description for unknown values. Five canonical
# levels match classic 5-act structure.
TENSION_SETUP = "setup"
TENSION_RISING = "rising"
TENSION_CLIMAX = "climax"
TENSION_FALLING = "falling"
TENSION_RESOLUTION = "resolution"

_VALID_TENSIONS = frozenset(
    {TENSION_SETUP, TENSION_RISING, TENSION_CLIMAX, TENSION_FALLING, TENSION_RESOLUTION},
)

# --- Beat status ------------------------------------------------------
# - pending: scheduled_date > today or waiting to be performed
# - active: reserved for legacy rows; treated like pending by readers
# - realized: performed in conversation/proactive and turned into a StoryEvent
# - skipped: operator deleted or arc was abandoned
BEAT_PENDING = "pending"
BEAT_ACTIVE = "active"
BEAT_REALIZED = "realized"
BEAT_SKIPPED = "skipped"

_VALID_BEAT_STATUSES = frozenset(
    {BEAT_PENDING, BEAT_ACTIVE, BEAT_REALIZED, BEAT_SKIPPED},
)

# --- Play attempt results ---------------------------------------------
# Closed vocabulary written by our own staging paths (never by a model or
# a user), so membership is a legitimate domain check rather than the
# keyword special-casing the LLM-first rule forbids.
#
# Two meanings live in ``last_play_attempt_result``: *what happened* to a
# staging attempt, and — since ``with_status`` also writes the field — how
# a beat reached a terminal status. Only the failure trio charges the
# autonomous retry budget (BACKGROUND_COST_CONTROL_PLAN §10 #3); anything
# else ("prompted", "notification_candidate", "scene_written:<strategy>",
# "realized", "skipped") is bookkeeping the LLM reads.
PLAY_RESULT_FAILED = "failed"
"""Autonomous scene run produced nothing and no finer cause was known."""
PLAY_RESULT_WRITER_CRASHED = "writer_crashed"
"""The scene writer raised."""
PLAY_RESULT_EMPTY_SCENE = "empty_scene"
"""The scene writer returned an empty narrative."""
PLAY_RESULT_RETRY_EXHAUSTED = "retry_exhausted"
"""Terminal marker written by the retirement path when the failure budget
runs out. Not an attempt result — it rides along a ``skipped`` status
flip, so it never charges the budget it is the consequence of."""

FAILED_PLAY_RESULTS = frozenset(
    {PLAY_RESULT_FAILED, PLAY_RESULT_WRITER_CRASHED, PLAY_RESULT_EMPTY_SCENE},
)


def is_failed_play_result(result: object) -> bool:
    """Did this staging attempt fail (and therefore cost a retry)?

    Unknown values are *not* failures: over-suppression is the failure
    mode this classification exists to remove, and the retirement path
    below is the backstop for a genuinely dead beat. Callers must use the
    ``PLAY_RESULT_*`` constants so a new failure kind is a one-line
    addition to ``FAILED_PLAY_RESULTS`` rather than a silent free retry.
    """
    return _normalise_optional_label(result) in FAILED_PLAY_RESULTS

# --- Scene types ------------------------------------------------------
# Categorical hint for the expander prompt — drives tone & pacing of the
# realized narrative. Free string with a recognised set; unknown values
# fall back to "encounter" semantics in the prompt builder so a planner
# returning an off-list shade ("inner_monologue") still produces a usable
# beat instead of a validation error.
SCENE_ENCOUNTER = "encounter"
SCENE_REVELATION = "revelation"
SCENE_CONFLICT = "conflict"
SCENE_RESOLUTION = "resolution"
SCENE_INTERLUDE = "interlude"

_VALID_SCENE_TYPES = frozenset(
    {SCENE_ENCOUNTER, SCENE_REVELATION, SCENE_CONFLICT, SCENE_RESOLUTION, SCENE_INTERLUDE},
)

# --- Operator position (OP0 of ARC_PLAYER_POSITION_PLAN) --------------
# Where the *player* stands in this scene. Until this slot existed a
# beat could only name third-party NPC labels in ``scene_characters``,
# so every consumer had to re-invent the player's place in the story
# from prose — which is exactly the "湊/卡" the plan diagnoses.
#
# Deliberately a **closed** vocabulary, unlike ``scene_type`` (which
# stays permissive so a planner can invent shades): consumers branch on
# this value — the autonomous beat scan skips ``central`` outright — so
# an off-vocabulary string would silently take a branch nobody chose.
OPERATOR_POSITION_ABSENT = "absent"
"""The player is not in this scene. A pure character-side beat that can
be played to completion without them."""
OPERATOR_POSITION_PRESENT = "present"
"""The player is in the scene but the scene is not *about* them — they
are there, accompanying, watching, reacting."""
OPERATOR_POSITION_CENTRAL = "central"
"""The scene is about the player and cannot be played without them.
Autonomous paths leave it alone; the player-facing exits play it."""

VALID_OPERATOR_POSITIONS = frozenset(
    {
        OPERATOR_POSITION_ABSENT,
        OPERATOR_POSITION_PRESENT,
        OPERATOR_POSITION_CENTRAL,
    },
)
"""The three legal values. ``None`` is **not** a member and is the
fourth, default state: *unjudged*. Every beat that existed before this
slot reads back as ``None`` (the migration deliberately backfills
nothing), and consumers answer ``None`` by deriving a framing
semantically rather than by assuming one."""

# --- Arc status -------------------------------------------------------
# - active: the current running arc (at most one per character, enforced
#   at service layer, not schema)
# - completed: all beats realized or last beat's date has passed
# - abandoned: explicitly dropped by operator
ARC_ACTIVE = "active"
ARC_COMPLETED = "completed"
ARC_ABANDONED = "abandoned"

_VALID_ARC_STATUSES = frozenset({ARC_ACTIVE, ARC_COMPLETED, ARC_ABANDONED})


def normalise_operator_position(value: object) -> str | None:
    """Canonical form of an operator-position value, or raise.

    ``None`` / blank → ``None`` (*unjudged*); a known label in any
    casing → the canonical lowercase constant; anything else raises
    ``ValueError``. Shared by ``StoryArcBeat`` and ``ArcTemplateBeat``
    so the runtime beat and the blueprint beat cannot drift apart —
    the same "Same normalisation rules — keep both sides symmetric"
    contract ``scene_characters`` already carries.

    Strict on purpose at the domain boundary. Ingest paths that must
    survive garbage (an LLM planner's response, a row written by an
    older schema) catch this and degrade to ``None``; they do not get
    to invent a fourth position.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(
            "operator_position must be a string or None, got "
            f"{type(value).__name__}",
        )
    cleaned = value.strip().lower()
    if not cleaned:
        return None
    if cleaned not in VALID_OPERATOR_POSITIONS:
        raise ValueError(
            f"operator_position {value!r} must be one of "
            f"{sorted(VALID_OPERATOR_POSITIONS)} or None (unjudged)",
        )
    return cleaned


def normalise_operator_note(value: object) -> str | None:
    """Free-text note about the player's dramatic place in this scene.

    Coerce-don't-reject (mirrors ``_normalise_optional_label``): blank
    and non-string collapse to ``None``. Prose, not vocabulary — it is
    what a writer prompt reads ("她要向你坦白"), so it is never
    validated against a list and never branched on.
    """
    return _normalise_optional_label(value)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class StoryArcBeat:
    """A single plot beat inside an arc.

    ``scheduled_date`` is the civil date (character local tz) when this
    beat should materialize as a ``StoryEvent``. Service-level logic
    picks up beats on or before today and realizes them. Beats can be
    pulled forward / pushed back freely while still ``pending``.
    """

    id: str
    arc_id: str
    sequence: int
    """Ordering within the arc; stable even when dates shift."""
    scheduled_date: date
    title: str
    summary: str
    """One paragraph scene seed. Prompt builders and post-turn LLM use it
    to stage/recognize the beat; the final StoryEvent narrative should
    describe what actually happened in the interaction."""
    tension: str = TENSION_SETUP
    status: str = BEAT_PENDING
    realized_event_id: str | None = None
    # --- Scene structure (Phase 1 of SCENE_BEAT_PLAN) ---------------
    # These fields make a beat into a *playable scene* rather than a
    # one-paragraph summary. The expander uses them to compose a
    # structured "演出這場戲" prompt; the prompt builder uses them to
    # raise today's beat from informational ("接下來節奏") to
    # directive ("今日場景指引"). All optional — old beats persisted
    # before Phase 1 read back with defaults and degrade gracefully.
    scene_characters: tuple[str, ...] = ()
    """Out-of-band character/NPC labels that should appear in this scene.
    Plain strings, not entity refs — the NPC system is out of scope."""
    location: str | None = None
    """Scene setting ("學校頂樓"/"咖啡廳吧檯"). ``None`` = unspecified
    (expander picks a sensible spot from character context)."""
    dramatic_question: str | None = None
    """The conflict/decision the scene revolves around. ``None`` = beat
    is a tonal interlude with no specific stakes."""
    scene_type: str = SCENE_ENCOUNTER
    required: bool = True
    """``True`` = main-line beat that must play on its scheduled date.
    ``False`` = optional/colour beat that can be skipped without
    breaking the arc — used by future template authoring + by the
    expander as a "soft" hint."""
    play_attempt_count: int = 0
    """How many times the system has tried to bring this beat into an
    interaction without it becoming realized yet. This is bookkeeping
    for LLM decisions, not a hardcoded threshold."""
    last_play_attempt_at: datetime | None = None
    last_play_attempt_source: str | None = None
    """Where the latest attempt came from, e.g. ``chat_scene_directive``
    or ``proactive_tick``."""
    last_play_attempt_result: str | None = None
    """Short factual outcome from the previous attempt: prompted,
    delayed, skipped, realized, etc."""
    last_play_push_intensity: str | None = None
    """Factual label for how strongly the last attempt tried to surface
    the beat. The LLM decides whether to escalate from this fact."""
    play_failure_count: int = 0
    """How many staging attempts actually *failed* (see
    ``FAILED_PLAY_RESULTS``). Separate from ``play_attempt_count``
    because that one answers "how often was this surfaced" — a question
    a chatty player inflates — while the autonomous retry budget must
    answer "how often did we try and fail"."""
    last_play_failure_at: datetime | None = None
    """When the last *failed* attempt happened. The retry backoff anchors
    here so a later non-failing attempt cannot push the retry window
    out."""
    # --- Player's place in this scene (OP0) --------------------------
    # ``scene_characters`` is the NPC cast and says nothing about the
    # player; these two say where the player stands. Kept as the last
    # fields so no positional construction anywhere shifts.
    operator_position: str | None = None
    """One of ``VALID_OPERATOR_POSITIONS``, or ``None`` = unjudged.
    Structural — consumers branch on it, and it is never translated."""
    operator_note: str | None = None
    """Optional one-line prose about *how* the player figures in this
    scene ("她要向你坦白"). Player-visible natural language: material for
    writer prompts, and a translation candidate."""

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("StoryArcBeat.id must be non-empty")
        if not self.arc_id:
            raise ValueError("StoryArcBeat.arc_id must be non-empty")
        if self.sequence < 0:
            raise ValueError("StoryArcBeat.sequence must be >= 0")
        if not self.title.strip():
            raise ValueError("StoryArcBeat.title must be non-empty")
        if not self.summary.strip():
            raise ValueError("StoryArcBeat.summary must be non-empty")
        if self.status not in _VALID_BEAT_STATUSES:
            raise ValueError(
                f"StoryArcBeat.status {self.status!r} must be one of "
                f"{sorted(_VALID_BEAT_STATUSES)}",
            )
        # scene_type is intentionally permissive — unknown values fall
        # back to encounter semantics in the prompt builder. We only
        # reject empty string so persisted rows always carry a label.
        if not self.scene_type or not self.scene_type.strip():
            raise ValueError("StoryArcBeat.scene_type must be non-empty")
        # tuple[str, ...] frozen-dataclass guarantees immutability, but
        # we still defensively reject non-string entries so a planner
        # bug producing nested lists doesn't silently corrupt prompts.
        for entry in self.scene_characters:
            if not isinstance(entry, str) or not entry.strip():
                raise ValueError(
                    "StoryArcBeat.scene_characters entries must be "
                    "non-empty strings",
                )
        if self.play_attempt_count < 0:
            raise ValueError("StoryArcBeat.play_attempt_count must be >= 0")
        if self.play_failure_count < 0:
            raise ValueError("StoryArcBeat.play_failure_count must be >= 0")
        object.__setattr__(
            self,
            "last_play_attempt_source",
            _normalise_optional_label(self.last_play_attempt_source),
        )
        object.__setattr__(
            self,
            "last_play_attempt_result",
            _normalise_optional_label(self.last_play_attempt_result),
        )
        object.__setattr__(
            self,
            "last_play_push_intensity",
            _normalise_optional_label(self.last_play_push_intensity),
        )
        # Raises on an off-vocabulary position — see
        # ``normalise_operator_position``. ``dataclasses.replace`` runs
        # __post_init__ too, so every ``with_*`` helper is covered.
        object.__setattr__(
            self,
            "operator_position",
            normalise_operator_position(self.operator_position),
        )
        object.__setattr__(
            self,
            "operator_note",
            normalise_operator_note(self.operator_note),
        )

    @classmethod
    def create(
        cls,
        *,
        arc_id: str,
        sequence: int,
        scheduled_date: date,
        title: str,
        summary: str,
        tension: str = TENSION_SETUP,
        status: str = BEAT_PENDING,
        realized_event_id: str | None = None,
        scene_characters: Iterable[str] = (),
        location: str | None = None,
        dramatic_question: str | None = None,
        scene_type: str = SCENE_ENCOUNTER,
        required: bool = True,
        play_attempt_count: int = 0,
        last_play_attempt_at: datetime | None = None,
        last_play_attempt_source: str | None = None,
        last_play_attempt_result: str | None = None,
        last_play_push_intensity: str | None = None,
        play_failure_count: int = 0,
        last_play_failure_at: datetime | None = None,
        operator_position: str | None = None,
        operator_note: str | None = None,
        id: str | None = None,
    ) -> StoryArcBeat:
        resolved_tension = tension.strip() or TENSION_SETUP
        resolved_scene_type = (scene_type or "").strip() or SCENE_ENCOUNTER
        # Strip + dedupe scene_characters preserving order; LLM planners
        # occasionally repeat the same name when listing participants.
        seen: set[str] = set()
        deduped: list[str] = []
        for raw in scene_characters:
            label = (raw or "").strip()
            if not label or label in seen:
                continue
            seen.add(label)
            deduped.append(label)
        normalized_location = (location or "").strip() or None
        normalized_question = (dramatic_question or "").strip() or None
        return cls(
            id=id or uuid4().hex,
            arc_id=arc_id,
            sequence=sequence,
            scheduled_date=scheduled_date,
            title=title.strip(),
            summary=summary.strip(),
            tension=resolved_tension,
            status=status,
            realized_event_id=realized_event_id,
            scene_characters=tuple(deduped),
            location=normalized_location,
            dramatic_question=normalized_question,
            scene_type=resolved_scene_type,
            required=bool(required),
            play_attempt_count=max(0, int(play_attempt_count)),
            last_play_attempt_at=last_play_attempt_at,
            last_play_attempt_source=_normalise_optional_label(
                last_play_attempt_source,
            ),
            last_play_attempt_result=_normalise_optional_label(
                last_play_attempt_result,
            ),
            last_play_push_intensity=_normalise_optional_label(
                last_play_push_intensity,
            ),
            play_failure_count=max(0, int(play_failure_count)),
            last_play_failure_at=last_play_failure_at,
            # Normalised (and validated) in __post_init__ — same as the
            # attempt labels above, so create() and a bare constructor
            # cannot disagree about what a legal position is.
            operator_position=operator_position,
            operator_note=operator_note,
        )

    def with_status(
        self,
        status: str,
        *,
        realized_event_id: str | None = None,
        play_result: str | None = None,
    ) -> StoryArcBeat:
        next_event_id = (
            realized_event_id
            if realized_event_id is not None
            else self.realized_event_id
        )
        return replace(
            self,
            status=status,
            realized_event_id=next_event_id,
            last_play_attempt_result=(
                _normalise_optional_label(play_result)
                if play_result is not None
                else self.last_play_attempt_result
            ),
        )

    def with_play_attempt(
        self,
        *,
        attempted_at: datetime,
        source: str,
        result: str,
        push_intensity: str,
    ) -> StoryArcBeat:
        """Record a staging attempt.

        ``play_attempt_count`` / ``last_play_attempt_*`` keep their
        original "this beat was surfaced" meaning — the prompt layer
        renders them and every source bumps them. The failure counters
        move only when ``result`` is a real failure, which is what the
        retry budget spends.
        """
        failed = is_failed_play_result(result)
        return replace(
            self,
            play_attempt_count=self.play_attempt_count + 1,
            last_play_attempt_at=attempted_at,
            last_play_attempt_source=_normalise_optional_label(source),
            last_play_attempt_result=_normalise_optional_label(result),
            last_play_push_intensity=_normalise_optional_label(push_intensity),
            play_failure_count=self.play_failure_count + (1 if failed else 0),
            last_play_failure_at=(
                attempted_at if failed else self.last_play_failure_at
            ),
        )

    def with_fields(
        self,
        *,
        scheduled_date: date | None = None,
        title: str | None = None,
        summary: str | None = None,
        tension: str | None = None,
        scene_characters: Iterable[str] | None = None,
        location: str | None = None,
        dramatic_question: str | None = None,
        scene_type: str | None = None,
        required: bool | None = None,
        operator_position: str | None = None,
        operator_note: str | None = None,
    ) -> StoryArcBeat:
        """Patch scene fields. ``None`` means *leave unchanged*.

        That sentinel collides with ``operator_position``'s legitimate
        ``None`` (*unjudged*), so clearing the slot uses the same escape
        hatch ``location`` / ``dramatic_question`` already use: pass
        ``""``. An editor that wants "back to unjudged" sends a blank
        string, not a missing key.
        """
        if scene_characters is not None:
            seen: set[str] = set()
            deduped: list[str] = []
            for raw in scene_characters:
                label = (raw or "").strip()
                if not label or label in seen:
                    continue
                seen.add(label)
                deduped.append(label)
            new_scene_characters: tuple[str, ...] = tuple(deduped)
        else:
            new_scene_characters = self.scene_characters
        if location is not None:
            cleaned_location = location.strip() or None
        else:
            cleaned_location = self.location
        if dramatic_question is not None:
            cleaned_question = dramatic_question.strip() or None
        else:
            cleaned_question = self.dramatic_question
        if scene_type is not None:
            cleaned_scene_type = scene_type.strip() or self.scene_type
        else:
            cleaned_scene_type = self.scene_type
        if operator_position is not None:
            # Validation stays in __post_init__ (via replace) so an
            # illegal patch is rejected the same way an illegal
            # construction is.
            next_position = operator_position
        else:
            next_position = self.operator_position
        if operator_note is not None:
            next_note = normalise_operator_note(operator_note)
        else:
            next_note = self.operator_note
        return replace(
            self,
            scheduled_date=scheduled_date if scheduled_date is not None else self.scheduled_date,
            title=title.strip() if title is not None else self.title,
            summary=summary.strip() if summary is not None else self.summary,
            tension=(tension.strip() or self.tension) if tension is not None else self.tension,
            scene_characters=new_scene_characters,
            location=cleaned_location,
            dramatic_question=cleaned_question,
            scene_type=cleaned_scene_type,
            required=self.required if required is None else bool(required),
            operator_position=next_position,
            operator_note=next_note,
        )


DEFAULT_ARC_TONE = "daily"
"""Tonal register surfaced to the expander; mirrors
``ArcTemplate.tone`` so an arc materialised from a template carries
the same vibe through to the runtime narrative. LLM-planned arcs
(no template) default to ``daily`` for backwards compatibility with
pre-tone behaviour."""


@dataclass(frozen=True, slots=True)
class StoryArc:
    id: str
    character_id: str
    title: str
    premise: str
    """Two-to-four sentence framing of the overall plot. Drives prompt
    context ("you're in the middle of ...") and beat regeneration."""
    theme: str
    """Free-text categorical (``ambition``/``friendship``/``loss``/
    ``discovery``/``custom``). Used by the planner for tonal coherence."""
    start_date: date
    end_date: date
    status: str = ARC_ACTIVE
    beats: tuple[StoryArcBeat, ...] = ()
    tone: str = DEFAULT_ARC_TONE
    """Tonal register driving expander prompt selection (daily /
    dramatic / mature / dark / lighthearted). Set from
    ``ArcTemplate.tone`` at materialise time; falls back to ``daily``
    for LLM-planned arcs that don't carry one."""
    source_template_id: str | None = None
    """Template id this runtime arc was materialised from.

    ``None`` means the arc came from the LLM planner or an ad-hoc user
    request. The service uses this as bookkeeping to prevent a completed
    template from automatically respawning forever.
    """
    source_seed_ids: tuple[str, ...] = ()
    """``StorySeed.id`` provenance for seeds the planner folded into this
    arc's premise/beats. Purely bookkeeping (AE2 slice of the arc-planner
    seed-injection plan) — nothing here reads or writes it yet; a later
    ticket has the LLM planner populate it and the service read it back
    to exclude already-used seeds from future rolls. Normalised on every
    construction (see ``_normalise_source_seed_ids``) so a misbehaving
    planner response cannot balloon the row or store garbage entries.
    """
    created_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("StoryArc.id must be non-empty")
        if not self.character_id:
            raise ValueError("StoryArc.character_id must be non-empty")
        if not self.title.strip():
            raise ValueError("StoryArc.title must be non-empty")
        if not self.premise.strip():
            raise ValueError("StoryArc.premise must be non-empty")
        if self.end_date < self.start_date:
            raise ValueError("StoryArc.end_date must be on or after start_date")
        if self.status not in _VALID_ARC_STATUSES:
            raise ValueError(
                f"StoryArc.status {self.status!r} must be one of "
                f"{sorted(_VALID_ARC_STATUSES)}",
            )
        if not self.tone or not self.tone.strip():
            raise ValueError("StoryArc.tone must be non-empty")
        cleaned_source = _normalise_source_template_id(self.source_template_id)
        object.__setattr__(self, "source_template_id", cleaned_source)
        object.__setattr__(
            self,
            "source_seed_ids",
            _normalise_source_seed_ids(self.source_seed_ids),
        )

    @classmethod
    def create(
        cls,
        *,
        character_id: str,
        title: str,
        premise: str,
        theme: str,
        start_date: date,
        end_date: date,
        beats: Iterable[StoryArcBeat] = (),
        status: str = ARC_ACTIVE,
        id: str | None = None,
        tone: str = DEFAULT_ARC_TONE,
        source_template_id: str | None = None,
        source_seed_ids: Iterable[str] = (),
    ) -> StoryArc:
        now = _utcnow()
        return cls(
            id=id or uuid4().hex,
            character_id=character_id,
            title=title.strip(),
            premise=premise.strip(),
            theme=(theme or "").strip() or "custom",
            start_date=start_date,
            end_date=end_date,
            status=status,
            beats=tuple(sorted(beats, key=lambda b: (b.scheduled_date, b.sequence))),
            tone=(tone or "").strip() or DEFAULT_ARC_TONE,
            source_template_id=_normalise_source_template_id(source_template_id),
            source_seed_ids=_normalise_source_seed_ids(source_seed_ids),
            created_at=now,
            updated_at=now,
        )

    def with_beats(self, beats: Iterable[StoryArcBeat]) -> StoryArc:
        ordered = tuple(sorted(beats, key=lambda b: (b.scheduled_date, b.sequence)))
        return replace(self, beats=ordered, updated_at=_utcnow())

    def with_status(self, status: str) -> StoryArc:
        return replace(self, status=status, updated_at=_utcnow())

    def with_title_premise(
        self, *, title: str | None = None, premise: str | None = None,
        theme: str | None = None,
    ) -> StoryArc:
        return replace(
            self,
            title=title.strip() if title is not None else self.title,
            premise=premise.strip() if premise is not None else self.premise,
            theme=(theme.strip() or self.theme) if theme is not None else self.theme,
            updated_at=_utcnow(),
        )

    def beats_on(self, target: date) -> list[StoryArcBeat]:
        """Pending / active beats whose scheduled_date matches ``target``."""
        return [
            b for b in self.beats
            if b.scheduled_date == target
            and b.status in (BEAT_PENDING, BEAT_ACTIVE)
        ]

    def forward_beats(
        self, *, after: date, limit: int = 2, include_today: bool = True,
    ) -> list[StoryArcBeat]:
        """Upcoming (non-realized, non-skipped) beats for prompt forward-feed.

        Ordered by scheduled_date + sequence. ``include_today`` controls
        whether today's pending beats are included — the prompt builder
        usually wants them (to announce "today:"), but some callers only
        want strictly-future beats.
        """
        threshold_cmp = (
            (lambda d: d >= after) if include_today else (lambda d: d > after)
        )
        upcoming = [
            b for b in self.beats
            if threshold_cmp(b.scheduled_date)
            and b.status in (BEAT_PENDING, BEAT_ACTIVE)
        ]
        upcoming.sort(key=lambda b: (b.scheduled_date, b.sequence))
        return upcoming[:limit]

    def realized_history_beats(self, *, limit: int = 5) -> list[StoryArcBeat]:
        """Key realized beats for deterministic arc-history prompt grounding."""
        if limit <= 0:
            return []
        realized = [b for b in self.beats if b.status == BEAT_REALIZED]
        if not realized:
            return []
        high_tension = [
            b for b in realized if b.tension in (TENSION_CLIMAX, TENSION_RESOLUTION)
        ]
        recent = sorted(realized, key=lambda b: (b.scheduled_date, b.sequence))[-limit:]
        selected: dict[str, StoryArcBeat] = {}
        for beat in [*high_tension, *recent]:
            selected[beat.id] = beat
        ordered = sorted(selected.values(), key=lambda b: (b.scheduled_date, b.sequence))
        return ordered[-limit:]

    def find_beat(self, beat_id: str) -> StoryArcBeat | None:
        for b in self.beats:
            if b.id == beat_id:
                return b
        return None

    def all_realized_or_skipped(self) -> bool:
        return all(
            b.status in (BEAT_REALIZED, BEAT_SKIPPED) for b in self.beats
        ) if self.beats else False


def _normalise_source_template_id(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


MAX_SOURCE_SEED_ID_LENGTH = 128
"""Defensive per-id clamp — story seed ids are uuid4 hex (32 chars) or
short pack slugs today; 128 gives headroom without letting a malformed
LLM response turn one entry into an unbounded string."""

MAX_SOURCE_SEED_IDS = 8
"""Cap on how many seed ids a single arc can carry provenance for. An
arc only has a handful of beats, so a planner returning far more than
this is misbehaving, not legitimately thorough."""


def _normalise_source_seed_ids(value: object) -> tuple[str, ...]:
    """Best-effort coercion of arc→seed provenance ids.

    Defensive against LLM planner garbage rather than validating and
    rejecting (mirrors ``_normalise_source_template_id``'s coerce-not-
    raise stance): non-iterable input becomes ``()``, non-string entries
    are dropped, each id is stripped and clamped to
    ``MAX_SOURCE_SEED_ID_LENGTH`` chars, duplicates are removed while
    preserving first-seen order, and the result is capped at
    ``MAX_SOURCE_SEED_IDS`` entries.
    """
    if value is None:
        return ()
    if isinstance(value, (str, bytes)):
        # A bare string is a caller mistake (meant a single id), not an
        # intentional request to iterate it character by character.
        candidates: Iterable[object] = (value,)
    else:
        try:
            candidates = list(value)
        except TypeError:
            return ()
    seen: set[str] = set()
    result: list[str] = []
    for raw in candidates:
        if not isinstance(raw, str):
            continue
        cleaned = raw.strip()[:MAX_SOURCE_SEED_ID_LENGTH]
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        result.append(cleaned)
        if len(result) >= MAX_SOURCE_SEED_IDS:
            break
    return tuple(result)


def _normalise_optional_label(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None
