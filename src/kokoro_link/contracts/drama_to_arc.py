"""Ports for adapting a played branching-drama path into an arc draft.

BD7. A branching drama is a *tree*; a playthrough is the one line the
player actually walked through it. That line — the beats they arrived at,
in the order they arrived, with what they said and what came back — is
the thing worth keeping, and it is materially different from a fusion
story: nobody wrote it in advance, and half of it is the player's own
input.

Deliberately a separate contract from :mod:`kokoro_link.contracts.
fusion_to_arc` rather than a widened one: the source material differs in
kind (a transcript with the player in it, not prose about characters),
and the mode is *pre-answered* by the drama's own ``operator_position``
instead of being asked cold. What the two share on purpose is the closed
three-way mode vocabulary — a creator converting either kind of finished
work is answering the same question ("how do I enter the arc this
becomes?"), and forking that vocabulary would fork the prompt blocks and
the frontend chips with it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from kokoro_link.application.services.arc_template_intake_service import (
    TemplateDraft,
)
from kokoro_link.contracts.fusion_to_arc import (
    DEFAULT_FUSION_OPERATOR_MODE,
    FUSION_OPERATOR_MODE_OBSERVER,
    FUSION_OPERATOR_MODE_UNCHANGED,
    FUSION_OPERATOR_MODE_WRITE_IN,
)
from kokoro_link.domain.entities.branching_drama import (
    BranchingDrama,
    Exchange,
    OPERATOR_POSITION_ABSENT,
    OPERATOR_POSITION_CENTRAL,
    OPERATOR_POSITION_PRESENT,
)
from kokoro_link.domain.entities.character import Character


# --- drama position → conversion mode ---------------------------------
#
# The player already answered "where do I stand in this?" on the create
# form, and the whole tree was planned against that answer. So unlike the
# fusion path — where an unstated mode means nobody chose and the
# conservative branch wins — an unstated mode here has a *better* answer
# available than a blanket default: the framing this drama was actually
# played under.
#
# The mapping is the semantic alignment of two closed vocabularies, not a
# guess:
#   central  → write_in   the drama was about the player; the arc it
#                         becomes should keep them in it.
#   present  → observer   they stood in the scene without it being about
#                         them, which is exactly what observer frames.
#   absent   → unchanged  they watched characters play; 紅線 5 says the
#                         adaptation must not write them in now.
#
# It is a pre-fill, never a lock: the exit hub shows all three chips and
# a player who watched a drama may still ask to be written into the arc.
_POSITION_TO_MODE: dict[str, str] = {
    OPERATOR_POSITION_CENTRAL: FUSION_OPERATOR_MODE_WRITE_IN,
    OPERATOR_POSITION_PRESENT: FUSION_OPERATOR_MODE_OBSERVER,
    OPERATOR_POSITION_ABSENT: FUSION_OPERATOR_MODE_UNCHANGED,
}


def operator_mode_for_drama_position(position: object) -> str:
    """Conversion mode implied by the drama's own player position.

    An unknown / missing position lands on
    :data:`~kokoro_link.contracts.fusion_to_arc.DEFAULT_FUSION_OPERATOR_MODE`
    rather than raising: this is the *pre-fill* path, reached when the
    caller said nothing, and the honest answer to "we cannot tell where
    the player stood" is the mode that leaves the story alone.
    """
    if not isinstance(position, str):
        return DEFAULT_FUSION_OPERATOR_MODE
    return _POSITION_TO_MODE.get(
        position.strip().lower(), DEFAULT_FUSION_OPERATOR_MODE,
    )


@dataclass(frozen=True, slots=True)
class DramaPathBeat:
    """One beat of the line the player walked, in play order.

    Node title / summary are the *authored* beat; ``narration`` and
    ``exchanges`` are what that beat became when this particular player
    walked into it. Both travel: the outline says what the beat was for,
    the transcript says what actually happened in it, and an adaptation
    that saw only one of the two would either flatten the playthrough
    back into the tree or lose the shape the tree gave it.

    There is deliberately no per-beat ``player_input``: everything the
    player said lives in ``exchanges``. The drama service never writes a
    turn-level input (advancing is a button press, and the words that
    earned the branch are the exchanges of the beat the player was
    standing in), so a beat-level field could only ever have been empty —
    which is worse than absent, because the prompt would keep announcing
    a slot that never carries anything (FX3).
    """

    sequence: int
    depth: int
    tone: str | None
    title: str
    summary: str
    narration: str
    exchanges: tuple[Exchange, ...] = ()


@dataclass(frozen=True, slots=True)
class DramaToArcContext:
    """Semantic source material for one drama-path adaptation call."""

    drama: BranchingDrama
    path: tuple[DramaPathBeat, ...]
    characters: tuple[Character, ...] = ()
    operator_primary_language: str = "zh-TW"
    instruction: str = ""
    operator_mode: str = DEFAULT_FUSION_OPERATOR_MODE
    """Which of the three conversion choices this call runs under —
    pre-filled from the drama's position when the caller said nothing."""
    operator_relationship_lines: tuple[str, ...] = field(default=())
    """Pre-rendered facts about who the player is to this cast. Same
    contract and same fail-soft as the fusion path: the application layer
    decides which facts travel, the prompt says what they are for, and
    ``unchanged`` sends nothing on purpose."""


class DramaToArcAdapterPort(Protocol):
    async def adapt(self, context: DramaToArcContext) -> TemplateDraft | None:
        """Return a reviewable template draft, or ``None`` on fail-soft."""
