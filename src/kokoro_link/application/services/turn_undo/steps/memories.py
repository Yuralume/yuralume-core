"""Delete the memory rows the turn's post-turn extraction wrote."""

from __future__ import annotations

from kokoro_link.application.services.turn_undo.result import UndoTally
from kokoro_link.application.services.turn_undo.step import (
    UndoContext, UndoStep,
)


class MemoryDeleteStep(UndoStep):
    """Memories are append-only, so a time window keyed on
    ``turn_started_at`` is enough: within this conversation there is no
    row after that instant the turn did not put there."""

    name = "memories"

    async def apply(self, context: UndoContext, tally: UndoTally) -> None:
        journal = context.journal
        tally.deleted_memories = (
            await context.deps.memories.delete_created_since(
                journal.conversation_id, journal.turn_started_at,
            )
        )
