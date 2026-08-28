"""TU2 — record the tombstone that shuts an in-flight post-turn down.

The turn's post-turn extraction may still be running when the undo
lands: a background task in embedded mode, a claimed worker job in
hosted. Neither can be waited for from here, so the rollback leaves a
durable marker instead and the post-turn checks it before it writes.
The protocol — the row, the retention window and the reading side —
lives in :mod:`kokoro_link.application.services.undone_turn_gate`; this
step is only the point at which the undo raises it.

**It runs first in the registry**, and that is not a preference: a gate
raised after the deletes leaves a window in which the post-turn writes
between the last delete and the marker, which is precisely the failure
it exists to prevent.
"""

from __future__ import annotations

from kokoro_link.application.services.turn_undo.result import UndoTally
from kokoro_link.application.services.turn_undo.step import (
    UndoContext, UndoStep,
)
from kokoro_link.application.services.undone_turn_gate import UndoneTurnGate


class TombstoneStep(UndoStep):
    name = "tombstone"

    async def apply(self, context: UndoContext, tally: UndoTally) -> None:
        gate = UndoneTurnGate(context.deps.undone_turns)
        # ``turn_record_id is None`` — a busy-defer turn, which mints no
        # turn record and runs no post-turn — leaves the tally ``False``:
        # nothing was in flight, so nothing needed gating.
        tally.recorded_tombstone = await gate.record(
            turn_record_id=context.journal.turn_record_id,
            conversation_id=context.journal.conversation_id,
            now=context.now,
        )
