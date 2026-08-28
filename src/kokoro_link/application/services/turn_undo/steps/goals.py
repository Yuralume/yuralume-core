"""Restore the character's goal list from the pre-turn snapshot."""

from __future__ import annotations

from kokoro_link.application.services.turn_snapshot_codec import (
    goal_from_dict,
)
from kokoro_link.application.services.turn_undo.result import UndoTally
from kokoro_link.application.services.turn_undo.step import (
    UndoContext, UndoStep,
)


class GoalRestoreStep(UndoStep):
    """Delete-all + bulk-insert, because the snapshot is the whole list.

    An empty snapshot is a real state ("no goals before the turn"), not
    a missing one, so it still clears whatever the turn added.
    """

    name = "goals"

    async def apply(self, context: UndoContext, tally: UndoTally) -> None:
        repository = context.deps.goals
        if repository is None:
            return
        journal = context.journal
        await repository.delete_for_character(journal.character_id)
        if journal.prev_goals:
            await repository.add_many(
                [goal_from_dict(goal) for goal in journal.prev_goals],
            )
        tally.restored_goals = True
