"""Ports for adapting ready fusion stories into arc-template drafts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from kokoro_link.application.services.arc_template_intake_service import (
    TemplateDraft,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.fusion_story import FusionStory

# --- Creator's choice: how the player enters this story (OP1-C) -------
#
# A fusion story is prose the creator wrote *about characters*; an arc is
# something the creator will then live through *with* a character. That
# gap is a question only the creator can answer, so ARC_PLAYER_POSITION_PLAN
# 拍板 #2 makes it an explicit three-way choice at conversion time rather
# than something the system infers ("創作意圖不該系統猜").
#
# Deliberately its own closed vocabulary and **not** reused from
# ``VALID_OPERATOR_POSITIONS``: a mode is an authoring instruction for one
# conversion call and is never persisted, while a position is a per-beat
# fact that lives on the beat forever. Overlapping the two literals would
# make a prompt that mentions both unreadable and invite code that treats
# "the mode" and "the beat's position" as the same thing — they differ by
# construction in ``write_in``, where the model judges each beat.
FUSION_OPERATOR_MODE_WRITE_IN = "write_in"
"""「把我寫進戲裡」 — the model re-imagines the beats so the player has a
real place in them (``central`` / ``present``, and ``absent`` where the
character deserves a scene of her own)."""
FUSION_OPERATOR_MODE_OBSERVER = "observer"
"""「我旁觀這個故事」 — the story stays exactly as written; the player is
framed as a witness who is there for it (``present``)."""
FUSION_OPERATOR_MODE_UNCHANGED = "unchanged"
"""「純角色戲保持原樣」 — a pure character story. Every beat is ``absent``
and the prose must not bend to make room for a player (紅線 5)."""

VALID_FUSION_OPERATOR_MODES = frozenset(
    {
        FUSION_OPERATOR_MODE_WRITE_IN,
        FUSION_OPERATOR_MODE_OBSERVER,
        FUSION_OPERATOR_MODE_UNCHANGED,
    },
)

DEFAULT_FUSION_OPERATOR_MODE = FUSION_OPERATOR_MODE_UNCHANGED
"""What an unstated mode means (ARC_PLAYER_POSITION_PLAN §3.2).

A caller that says nothing has not chosen — and of the three, exactly one
does not touch the creator's story: ``unchanged``. Defaulting to a rewrite
would mean an API client (or a browser still running the pre-OP1-C bundle)
that has never heard of this parameter gets its novel re-cast around a
player nobody asked for, which is the thing 紅線 5 exists to forbid. The
adapted beats are therefore the same adaptation the pre-OP1-C prompt
produced, plus the structural fact that the player is in none of them.

Note this is the *protocol* default, not the UI default: the conversion
screen pre-selects 「把我寫進戲裡」 and always sends an explicit mode, so a
creator is never silently opted into the conservative branch — they are
shown the choice."""


def normalise_fusion_operator_mode(value: object) -> str:
    """Canonical mode for one conversion call, or raise.

    ``None`` / blank → :data:`DEFAULT_FUSION_OPERATOR_MODE` (nobody chose);
    a known label in any casing → the canonical constant; anything else
    raises ``ValueError`` so the API surface can answer 422 instead of
    quietly adapting under a mode the caller did not ask for. Unlike a
    stored value there is no forgiving read path here: a mode is a live
    instruction, and guessing which of three creator intents an unknown
    string meant is precisely the silent guess 拍板 #2 rules out.
    """
    if value is None:
        return DEFAULT_FUSION_OPERATOR_MODE
    if not isinstance(value, str):
        raise ValueError(
            "operator_mode must be a string or None, got "
            f"{type(value).__name__}",
        )
    cleaned = value.strip().lower()
    if not cleaned:
        return DEFAULT_FUSION_OPERATOR_MODE
    if cleaned not in VALID_FUSION_OPERATOR_MODES:
        raise ValueError(
            f"operator_mode {value!r} must be one of "
            f"{sorted(VALID_FUSION_OPERATOR_MODES)}",
        )
    return cleaned


@dataclass(frozen=True, slots=True)
class FusionToArcContext:
    """Semantic source material for one fusion-story adaptation call."""

    story: FusionStory
    characters: tuple[Character, ...]
    operator_primary_language: str = "zh-TW"
    instruction: str = ""
    operator_mode: str = DEFAULT_FUSION_OPERATOR_MODE
    """Which of the three creator choices this call runs under (OP1-C)."""
    operator_relationship_lines: tuple[str, ...] = ()
    """Pre-rendered facts about who the player is to this story's cast —
    how each character addresses them, how close they stand. Same
    contract as the arc planner's input (OP1-A): the application layer
    picks *which* facts travel, the prompt says what they are for.

    Empty tuple means "we know nothing", which is also what the
    ``unchanged`` mode sends **on purpose**: a story the player is not in
    has no use for the player's name, and a prompt that never mentions
    them cannot be tempted to write them in."""


class FusionToArcAdapterPort(Protocol):
    async def adapt(self, context: FusionToArcContext) -> TemplateDraft | None:
        """Return a reviewable template draft, or ``None`` on fail-soft."""
