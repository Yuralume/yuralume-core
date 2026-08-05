"""Ports for the StoryArc layer.

Kept separate from ``contracts/story.py`` (seeds / events) so the arc
code can evolve without dragging the more mature gacha infrastructure
along. Arcs are optional — chat works fine without any arc repository
wired up; the orchestrator degrades to the existing gacha path.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.story_arc import StoryArc, StoryArcBeat
from kokoro_link.domain.entities.story_seed import StorySeed


class ActiveArcConflict(Exception):
    """Another writer already owns this character's single active-arc slot.

    Raised by ``add`` / ``save`` when the write would leave a character with
    two ``active`` arcs — i.e. when the DB's ``uq_story_arcs_active_character``
    partial unique index rejects it. It is a *benign race*, not a defect: the
    losing writer must adopt the winner (re-read ``get_active_for_character``)
    rather than overwrite it, because the winner's arc is what every reader
    surface is already showing.

    Implementations that cannot detect the race (in-memory) never raise it, so
    callers must treat it as an optional signal.
    """

    def __init__(self, character_id: str) -> None:
        super().__init__(
            f"character {character_id} already has an active story arc",
        )
        self.character_id = character_id


class StoryArcRepositoryPort(ABC):
    """CRUD for ``StoryArc`` + its embedded beats.

    Implementations persist arc + beats atomically: ``save`` replaces
    the arc row + wipes/rebuilds the beats so the caller only has to
    reason about the aggregate as a unit. Split updates (e.g. change one
    beat) still route through ``save`` — cheaper than a per-beat API
    for the scales we care about (3–7 beats per arc, <20 arcs per
    character over the product's lifetime).

    That convenience has one cost: ``save`` is last-writer-wins over the
    *whole* aggregate, so anything that landed since the caller read the
    arc is silently overwritten. Writers that must not do that use the
    narrow conditional operations (``skip_beats_if_pending``,
    ``complete_arc_if_all_terminal``) instead of loading, editing and
    saving the aggregate.

    At most ONE arc per character may be ``active``. DB-backed
    implementations enforce it with a partial unique index and surface a
    violation as :class:`ActiveArcConflict`.
    """

    @abstractmethod
    async def add(self, arc: StoryArc) -> None:
        """Insert a new arc.

        Raises :class:`ActiveArcConflict` when ``arc`` is active and the
        character already has an active arc."""

    @abstractmethod
    async def get(self, arc_id: str) -> StoryArc | None: ...

    @abstractmethod
    async def get_active_for_character(
        self, character_id: str,
    ) -> StoryArc | None: ...

    @abstractmethod
    async def list_for_character(
        self, character_id: str,
    ) -> list[StoryArc]: ...

    @abstractmethod
    async def save(self, arc: StoryArc) -> None:
        """Upsert — replaces the arc + all beats atomically.

        Raises :class:`ActiveArcConflict` when the write would make this a
        second active arc for the character."""

    @abstractmethod
    async def skip_beats_if_pending(
        self,
        arc_id: str,
        beat_ids: Sequence[str],
        *,
        play_result: str,
    ) -> int:
        """Flip the named beats ``pending`` → ``skipped``. Returns how many moved.

        The narrow escape hatch from the read-modify-``save`` cycle above:
        ``save`` rebuilds every beat row from the caller's snapshot, so a
        writer that landed since the snapshot was taken is overwritten.
        For the retirement path that meant a beat whose scene had already
        been performed *and charged* could be reverted to ``pending`` —
        canon loss, and the beat is playable (and payable) again.

        Implementations must apply this as ONE conditional statement: the
        ``pending`` predicate is the fence, not a check the caller did
        earlier. Beats already ``realized`` / ``skipped`` are left exactly
        as they are and simply don't count towards the return value, which
        is what makes a return of ``0`` mean "another writer got there
        first" rather than "nothing to do".

        ``play_result`` is written to ``last_play_attempt_result`` on the
        rows that move (e.g. ``retry_exhausted``); no other column, and no
        other beat, is touched."""

    @abstractmethod
    async def complete_arc_if_all_terminal(self, arc_id: str) -> bool:
        """Flip an ``active`` arc to ``completed`` iff every beat is terminal.

        Companion to ``skip_beats_if_pending``: closing the arc through
        ``save`` would reintroduce the very whole-aggregate overwrite that
        method exists to avoid. Evaluated server-side in one statement, so
        a beat realized between the caller's read and this call keeps the
        arc open instead of being erased by a stale snapshot.

        ``True`` only when this call performed the transition. An arc that
        is already terminal, still has a ``pending`` / ``active`` beat, has
        no beats at all, or does not exist returns ``False``."""

    @abstractmethod
    async def delete(self, arc_id: str) -> None: ...

    @abstractmethod
    async def delete_for_character(self, character_id: str) -> int: ...

    @abstractmethod
    async def find_by_beat_id(self, beat_id: str) -> StoryArc | None:
        """Reverse lookup: the arc containing this beat, or ``None``.

        Used by beat-level REST routes (``PATCH /story-arc-beats/{id}``)
        that don't have the parent arc id in the URL. Implementations
        can do a DB join or an in-memory scan — per-character arc
        counts stay in single digits so cost is negligible."""


class StoryArcPlannerPort(ABC):
    """Given a character + a start date, produce an arc with beats."""

    @abstractmethod
    async def plan_arc(
        self,
        *,
        character: Character,
        start_date: date,
        duration_days: int = 21,
        beat_count_hint: int = 5,
        hint: str | None = None,
        recent_dialogue_summary: str = "",
        operator_primary_language: str = "zh-TW",
        today: date | None = None,
        seed_candidates: tuple[StorySeed, ...] = (),
        arc_history: tuple[str, ...] = (),
        operator_relationship_lines: tuple[str, ...] = (),
    ) -> StoryArc:
        """Return a fresh ``StoryArc`` with beats scheduled between
        ``start_date`` and ``start_date + duration_days``. ``hint`` is
        optional free-text from the operator ("她要準備一場獨奏會").

        ``recent_dialogue_summary`` is an optional pre-condensed blurb of
        the character's latest chat with the user — lets the arc pick
        up whatever thread the conversation is already pulling on instead
        of starting cold. Empty string = no context available.

        ``today`` is the operator-local civil day the plan is being made
        on. It is usually equal to ``start_date`` but diverges on a
        mid-arc replan, where beats resume after the last realized one.
        Implementations use it to give the model absolute-date anchors so
        beat prose does not freeze relative time words ("明天") that go
        stale the moment the beat is read back a day later. Optional:
        implementations must degrade gracefully when it is ``None``.

        ``seed_candidates`` are ``dramatic``-tier story seeds offered as
        *subject-matter candidates* — raw material, never instructions.
        The planner is free to weave 0–2 of them into the arc, rewrite
        them beyond recognition, or ignore the lot; when it ignores them
        it still owes the arc an external event of its own. Empty tuple =
        no candidates available (empty pool, no gacha wired, or the roll
        failed), which must render exactly like the pre-seed prompt.

        ``arc_history`` is the anti-repetition input: pre-formatted
        one-line digests of this character's previous arcs, **oldest
        first**, already excluding whichever arc the call is about. The
        planner must keep the new arc's core conflict clearly distinct
        from every entry. Semantic judgement only — no keyword or
        similarity matching anywhere in this path.

        ``operator_relationship_lines`` are pre-rendered facts about the
        *player*: how the character addresses them, how they address the
        character, the relationship label and how close the two stand
        (OP1-A). Until this existed the planner knew the player only as
        a continuity constraint and could not even name them, which is
        why beats came back with the player structurally missing. Empty
        tuple = no relationship material recorded, and the prompt then
        omits the section entirely rather than rendering an empty
        heading — an unfilled slot is something a model will try to
        fill.

        The planner must always return a valid arc (at least one beat).
        On LLM failure, fall back to a sparse synthetic arc — the
        service layer treats a missing arc and an empty arc equally
        (both skip arc-driven event selection for the day).
        """


@dataclass(frozen=True, slots=True)
class StoryArcSeasonContext:
    """Facts for deciding whether a dormant character should open a new arc.

    The service passes bookkeeping and recent narrative facts only; the
    semantic call about rhythm and readiness belongs to the decider.
    """

    character: Character
    today: date
    completed_arc: StoryArc | None
    days_since_completed: int | None
    recent_dialogue_summary: str
    continuation_summary: str
    arc_history: tuple[str, ...] = ()
    """Pre-formatted one-line digests of this character's earlier arcs,
    oldest first, excluding ``completed_arc`` (which is passed whole).

    Anti-repetition input for the opener decision: the hint handed to the
    planner must not re-run any of these. Empty tuple = no history worth
    showing, and the decider prompt then omits the block entirely."""

    series_id: str | None = None
    series_title: str | None = None
    next_template_id: str | None = None
    next_template_title: str | None = None


@dataclass(frozen=True, slots=True)
class StoryArcSeasonDecision:
    should_start: bool
    reason: str
    hint: str | None = None


class StoryArcSeasonDeciderPort(ABC):
    """LLM-first season opener decider for dormant story arcs."""

    @abstractmethod
    async def decide(
        self, context: StoryArcSeasonContext,
    ) -> StoryArcSeasonDecision:
        """Return whether a new LLM-planned arc should start now."""


@dataclass(frozen=True, slots=True)
class StoryBeatRecheckContext:
    """Facts for judging a due beat that has been surfaced repeatedly.

    The service owns the threshold and state mutation. The LLM only
    answers whether the recent interaction actually fulfilled the beat,
    whether the beat should be delayed/skipped, or whether it should
    stay pending for a future turn.
    """

    character: Character
    arc: StoryArc
    beat: StoryArcBeat
    today: date
    recent_dialogue_summary: str = ""
    operator_primary_language: str = "zh-TW"


@dataclass(frozen=True, slots=True)
class StoryBeatRecheckDecision:
    action: str
    """One of keep_pending, delay_beat, skip_beat, mark_realized."""

    reason: str = ""
    days: int | None = None
    narrative: str | None = None


class StoryBeatRecheckerPort(ABC):
    """LLM-first semantic recheck for repeatedly surfaced arc beats."""

    @abstractmethod
    async def recheck(
        self, context: StoryBeatRecheckContext,
    ) -> StoryBeatRecheckDecision:
        """Return the narrow action the service may apply."""


@dataclass(frozen=True, slots=True)
class StoryBeatSceneContext:
    """Facts for turning one due arc beat into an autonomous scene.

    Direction C keeps the semantic decision inside the LLM prompt: the
    service passes structured beat facts and attempt history, and the
    writer decides whether the scene is best handled as inner monologue,
    NPC/companion dialogue, or a moment the player is present for. It
    must never wait for the user to be present in order to finish the
    beat.

    Where the player stands comes from ``beat.operator_position`` /
    ``beat.operator_note`` (OP0-A) — the writer reads them off the beat
    it is already given, so there is no separate context field to keep
    in sync. OP2-C retired the blanket involvement policy that used to
    live here; ``user_involvement_policy`` survives only as the *opt-in*
    override the simulate route accepts, empty by default.
    """

    character: Character
    arc: StoryArc
    beat: StoryArcBeat
    today: date
    operator_primary_language: str = "zh-TW"
    user_involvement_policy: str = ""
    """Caller-supplied extra directive, or ``""`` for none.

    Deliberately *not* a default policy any more: a default here is a
    guess about the player's place in a beat that the beat itself now
    answers. Rendered as an additional instruction that cannot loosen
    the no-invented-player-lines red line."""


@dataclass(frozen=True, slots=True)
class StoryBeatSceneDraft:
    narrative: str
    emotional_tone: str | None = None
    cast_strategy: str = "autonomous"
    participation_note: str = ""


class StoryBeatSceneWriterPort(ABC):
    """Write a short performed scene for a due arc beat."""

    @abstractmethod
    async def write_scene(
        self, context: StoryBeatSceneContext,
    ) -> StoryBeatSceneDraft:
        """Return the scene narrative that should become StoryEvent."""


@dataclass(frozen=True, slots=True)
class ArcCompletionMemoryContext:
    character: Character
    arc: StoryArc
    realized_beats: tuple[StoryArcBeat, ...]
    operator_primary_language: str = "zh-TW"
    today: date | None = None
    """Operator-local civil day the milestone is written on.

    Feeds the absolute-date anchors in the writer's prompt: the memory
    outlives the day it was written by months, so relative time words
    inside it never resolve. Optional so older callers keep working —
    the prompt then states the discipline without the anchor table."""


@dataclass(frozen=True, slots=True)
class ArcCompletionMemoryDraft:
    content: str


class ArcCompletionMemoryWriterPort(ABC):
    """Writes the relationship-milestone memory when an arc completes."""

    @abstractmethod
    async def write_memory(
        self, context: ArcCompletionMemoryContext,
    ) -> ArcCompletionMemoryDraft:
        """Return one concise long-term memory sentence."""
