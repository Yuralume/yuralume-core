"""Outline value objects for the fusion-story pipeline.

A ``FusionOutline`` is the LLM-produced 4-act plan (起 / 承 / 轉 / 合)
that drives the per-beat expansion stage. It is intentionally separate
from ``StoryArcBeat`` because:

- arc beats are scheduled on a calendar and tied to a single character;
  fusion-story beats are fictional acts in one short story across many
  characters.
- arc beats outlive their materialisation; outline beats are transient
  scaffolding that may be regenerated freely.

Frozen, hashable, validated at construction so the planner output never
leaks malformed shapes into the writer stage.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field, replace


ACT_OPENING = "opening"
ACT_RISING = "rising"
ACT_TURN = "turn"
ACT_RESOLUTION = "resolution"

CANONICAL_ACTS: tuple[str, ...] = (
    ACT_OPENING, ACT_RISING, ACT_TURN, ACT_RESOLUTION,
)
"""起 / 承 / 轉 / 合 — keeps the act labels stable across UI and prompts."""


@dataclass(frozen=True, slots=True)
class FusionBeatPlan:
    """A single act in the outline.

    ``target_chars`` is a soft hint to the writer ("aim for ~700 字")
    rather than a hard cap; the polish stage adjusts the final length.
    ``focus_character_ids`` are the subset of selected characters whose
    POV / agency this beat centres on — empty means "all of them".

    ``entry_state`` / ``exit_state`` / ``transition_from_previous`` are
    the *transition contract* between beats — empty strings when the
    planner didn't fill them (older outlines, fallback template). The
    writer treats non-empty values as hard guidance: an ``entry_state``
    of "翌日清晨，便利店外，從 B 的視角" means the prose must open
    there, not wherever the LLM felt like opening. Keeping them as
    free-form strings (vs an enum) lets the planner be expressive about
    unusual transitions (蒙太奇 / 內心獨白切回現實 / …) without
    forcing the writer to learn new symbols.
    """

    sequence: int
    act: str
    title: str
    hook: str
    """Two-sentence narrative seed describing what this act does."""
    dramatic_question: str
    target_chars: int
    focus_character_ids: tuple[str, ...] = field(default_factory=tuple)
    entry_state: str = ""
    """When/where/POV the beat opens at. Empty = planner didn't pin it."""
    exit_state: str = ""
    """When/where/POV the beat closes at. Fed to the next beat as
    承接 anchor."""
    transition_from_previous: str = ""
    """How this beat connects to the previous one — "直接承接" /
    "時間跳躍 N 小時/天" / "場景切換到 X" / "蒙太奇" / …. The first
    beat's value is ignored (no previous to bridge from)."""

    def __post_init__(self) -> None:
        if self.sequence < 0:
            raise ValueError("FusionBeatPlan.sequence must be >= 0")
        if not self.act or not self.act.strip():
            raise ValueError("FusionBeatPlan.act must be non-empty")
        if not self.title.strip():
            raise ValueError("FusionBeatPlan.title must be non-empty")
        if not self.hook.strip():
            raise ValueError("FusionBeatPlan.hook must be non-empty")
        if self.target_chars < 100:
            raise ValueError(
                "FusionBeatPlan.target_chars must be >= 100; got "
                f"{self.target_chars}",
            )

    @classmethod
    def create(
        cls,
        *,
        sequence: int,
        act: str,
        title: str,
        hook: str,
        dramatic_question: str = "",
        target_chars: int = 600,
        focus_character_ids: Iterable[str] = (),
        entry_state: str = "",
        exit_state: str = "",
        transition_from_previous: str = "",
    ) -> "FusionBeatPlan":
        cleaned_focus = tuple(
            cid.strip() for cid in focus_character_ids
            if isinstance(cid, str) and cid.strip()
        )
        return cls(
            sequence=sequence,
            act=act.strip().lower(),
            title=title.strip()[:80],
            hook=hook.strip()[:400],
            dramatic_question=dramatic_question.strip()[:160],
            target_chars=max(100, int(target_chars)),
            focus_character_ids=cleaned_focus,
            entry_state=entry_state.strip()[:240],
            exit_state=exit_state.strip()[:240],
            transition_from_previous=transition_from_previous.strip()[:240],
        )


@dataclass(frozen=True, slots=True)
class FusionOutline:
    """Full 4-act plan produced by the planner stage.

    The writer stage iterates beats in order and feeds each into a
    standalone LLM call along with a running summary of prior acts.
    """

    title: str
    premise: str
    theme: str
    beats: tuple[FusionBeatPlan, ...]

    def __post_init__(self) -> None:
        if not self.title.strip():
            raise ValueError("FusionOutline.title must be non-empty")
        if not self.premise.strip():
            raise ValueError("FusionOutline.premise must be non-empty")
        if not self.beats:
            raise ValueError("FusionOutline.beats must be non-empty")
        seen_seq: set[int] = set()
        for beat in self.beats:
            if beat.sequence in seen_seq:
                raise ValueError(
                    "FusionOutline.beats has duplicate sequence "
                    f"{beat.sequence}",
                )
            seen_seq.add(beat.sequence)

    @classmethod
    def create(
        cls,
        *,
        title: str,
        premise: str,
        theme: str = "custom",
        beats: Iterable[FusionBeatPlan],
    ) -> "FusionOutline":
        ordered = tuple(sorted(beats, key=lambda b: b.sequence))
        return cls(
            title=title.strip()[:120],
            premise=premise.strip()[:600],
            theme=(theme or "").strip() or "custom",
            beats=ordered,
        )

    def with_beats(
        self, beats: Iterable[FusionBeatPlan],
    ) -> "FusionOutline":
        ordered = tuple(sorted(beats, key=lambda b: b.sequence))
        return replace(self, beats=ordered)

    def total_target_chars(self) -> int:
        return sum(b.target_chars for b in self.beats)
