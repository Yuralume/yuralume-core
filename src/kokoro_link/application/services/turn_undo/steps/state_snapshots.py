"""Delete the state-history rows the turn appended."""

from __future__ import annotations

from kokoro_link.application.services.turn_undo.result import UndoTally
from kokoro_link.application.services.turn_undo.step import (
    UndoContext, UndoStep,
)


class StateSnapshotDeleteStep(UndoStep):
    """Same append-only reasoning as the memory step, scoped to the
    character rather than the conversation because state history is
    kept per character."""

    name = "state-snapshots"

    async def apply(self, context: UndoContext, tally: UndoTally) -> None:
        repository = context.deps.state_history
        if repository is None:
            return
        journal = context.journal
        tally.deleted_state_snapshots = await repository.delete_created_since(
            journal.character_id, journal.turn_started_at,
        )
