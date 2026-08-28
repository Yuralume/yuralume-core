"""Reject the persona evidence the turn staged."""

from __future__ import annotations

from kokoro_link.application.services.turn_undo.result import UndoTally
from kokoro_link.application.services.turn_undo.step import (
    UndoContext, UndoStep,
)


class PersonaEvidenceRejectStep(UndoStep):
    """Persona fields extracted from a reverted turn are *rejected*, not
    deleted: the staging buffer keeps its own record of what was
    proposed and why it never landed."""

    name = "persona-evidence"

    async def apply(self, context: UndoContext, tally: UndoTally) -> None:
        repository = context.deps.operator_persona
        if repository is None:
            return
        journal = context.journal
        tally.rejected_persona_fields = await repository.reject_evidence_since(
            conversation_id=journal.conversation_id,
            since=journal.turn_started_at,
        )
