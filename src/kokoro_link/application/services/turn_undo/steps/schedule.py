"""Restore today's daily schedule from the pre-turn snapshot."""

from __future__ import annotations

from kokoro_link.application.services.turn_snapshot_codec import (
    schedule_from_dict,
)
from kokoro_link.application.services.turn_undo.result import UndoTally
from kokoro_link.application.services.turn_undo.step import (
    UndoContext, UndoStep,
)


class ScheduleRestoreStep(UndoStep):
    """The snapshot is the whole local day (header + activities) and
    ``save`` replaces it wholesale, so a turn that rewrote an activity's
    description or flipped ``memorialized`` is reversed along with it."""

    name = "schedule"

    async def apply(self, context: UndoContext, tally: UndoTally) -> None:
        repository = context.deps.schedules
        journal = context.journal
        if repository is None or journal.prev_daily_schedule is None:
            return
        await repository.save(schedule_from_dict(journal.prev_daily_schedule))
        tally.restored_schedule = True
