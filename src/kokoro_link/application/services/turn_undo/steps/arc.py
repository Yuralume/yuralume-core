"""Restore the active story arc from the pre-turn snapshot."""

from __future__ import annotations

from kokoro_link.application.services.turn_snapshot_codec import arc_from_dict
from kokoro_link.application.services.turn_undo.result import UndoTally
from kokoro_link.application.services.turn_undo.step import (
    UndoContext, UndoStep,
)


class ArcRestoreStep(UndoStep):
    """``StoryArcRepositoryPort.save`` is an atomic whole-arc upsert, so
    writing the snapshot back restores the arc *and* its beats.

    Covers only the case where an arc existed before the turn. An arc
    the turn itself created has no snapshot to write back; that is
    ``CreatedArcDeleteStep``'s job, and it is a separate step precisely
    because telling the two situations apart needs ``had_active_arc``
    rather than the absence of a snapshot.
    """

    name = "arc"

    async def apply(self, context: UndoContext, tally: UndoTally) -> None:
        repository = context.deps.arcs
        journal = context.journal
        if repository is None or journal.prev_active_arc is None:
            return
        await repository.save(arc_from_dict(journal.prev_active_arc))
        tally.restored_arc = True
