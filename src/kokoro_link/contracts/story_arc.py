"""Ports for the StoryArc layer.

Kept separate from ``contracts/story.py`` (seeds / events) so the arc
code can evolve without dragging the more mature gacha infrastructure
along. Arcs are optional — chat works fine without any arc repository
wired up; the orchestrator degrades to the existing gacha path.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date

from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.story_arc import StoryArc, StoryArcBeat


class StoryArcRepositoryPort(ABC):
    """CRUD for ``StoryArc`` + its embedded beats.

    Implementations persist arc + beats atomically: ``save`` replaces
    the arc row + wipes/rebuilds the beats so the caller only has to
    reason about the aggregate as a unit. Split updates (e.g. change one
    beat) still route through ``save`` — cheaper than a per-beat API
    for the scales we care about (3–7 beats per arc, <20 arcs per
    character over the product's lifetime).
    """

    @abstractmethod
    async def add(self, arc: StoryArc) -> None: ...

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
        """Upsert — replaces the arc + all beats atomically."""

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
    ) -> StoryArc:
        """Return a fresh ``StoryArc`` with beats scheduled between
        ``start_date`` and ``start_date + duration_days``. ``hint`` is
        optional free-text from the operator ("她要準備一場獨奏會").

        ``recent_dialogue_summary`` is an optional pre-condensed blurb of
        the character's latest chat with the user — lets the arc pick
        up whatever thread the conversation is already pulling on instead
        of starting cold. Empty string = no context available.

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
    service passes structured beat facts, attempt history, and the user
    availability policy; the writer decides whether the scene is best
    handled as inner monologue, NPC/companion dialogue, or an implied
    off-screen user-adjacent moment. It must never wait for the user to
    be present in order to finish the beat.
    """

    character: Character
    arc: StoryArc
    beat: StoryArcBeat
    today: date
    operator_primary_language: str = "zh-TW"
    user_involvement_policy: str = (
        "使用者不一定在場；若不適合把使用者寫進場景，"
        "請用角色自己、scene_characters、companion 或 NPC label 完成。"
    )


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
