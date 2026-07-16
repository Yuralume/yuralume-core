"""Goal reviewer port.

The reviewer runs periodically (not every turn) to advance medium-term
goals: decide which active goals have progressed / completed / should be
abandoned, and whether new goals should emerge from recent conversation.

Separation from ``PostTurnProcessorPort`` is intentional — goals should
remain **stable** across turns; only the reviewer may mutate them, and
only at deliberate checkpoints.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.character_goal import CharacterGoal
from kokoro_link.domain.entities.conversation import Message
from kokoro_link.domain.value_objects.goal_status import GoalStatus


@dataclass(frozen=True, slots=True)
class GoalStatusChange:
    """Reviewer's verdict for an existing active goal."""

    goal_id: str
    new_status: GoalStatus
    notes: str | None = None


@dataclass(frozen=True, slots=True)
class NewGoalProposal:
    """Reviewer's proposal to create a brand-new goal."""

    content: str
    priority: int = 3
    tags: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class GoalReviewResult:
    status_changes: list[GoalStatusChange] = field(default_factory=list)
    new_goals: list[NewGoalProposal] = field(default_factory=list)


class GoalReviewerPort(Protocol):
    async def review(
        self,
        *,
        character: Character,
        active_goals: list[CharacterGoal],
        recent_messages: list[Message],
        operator_primary_language: str = "zh-TW",
    ) -> GoalReviewResult:
        """Judge each active goal and optionally propose new ones.

        ``operator_primary_language`` (BCP 47) is the content language for
        the player-visible ``NewGoalProposal.content`` and
        ``GoalStatusChange.notes`` — both render in PlayerGoalsPanel.vue,
        so a non-Chinese player must not see Chinese goals / review notes.
        Defaults to ``zh-TW`` (ship-first) so legacy callers keep working.
        """
