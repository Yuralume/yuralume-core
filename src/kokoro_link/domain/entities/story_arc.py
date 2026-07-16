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

# --- Arc status -------------------------------------------------------
# - active: the current running arc (at most one per character, enforced
#   at service layer, not schema)
# - completed: all beats realized or last beat's date has passed
# - abandoned: explicitly dropped by operator
ARC_ACTIVE = "active"
ARC_COMPLETED = "completed"
ARC_ABANDONED = "abandoned"

_VALID_ARC_STATUSES = frozenset({ARC_ACTIVE, ARC_COMPLETED, ARC_ABANDONED})


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
        return replace(
            self,
            play_attempt_count=self.play_attempt_count + 1,
            last_play_attempt_at=attempted_at,
            last_play_attempt_source=_normalise_optional_label(source),
            last_play_attempt_result=_normalise_optional_label(result),
            last_play_push_intensity=_normalise_optional_label(push_intensity),
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
    ) -> StoryArcBeat:
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


def _normalise_optional_label(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None
