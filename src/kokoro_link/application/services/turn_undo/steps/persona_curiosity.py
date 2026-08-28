"""TU6 — let the character ask again after the asking is undone.

``persona_curiosity_attempts`` is what stops the character asking the
same get-to-know-you question twice. Undo the turn it was asked in and
the attempt row survives, so the question is spent: never asked, never
answered, and never asked again.

Deletes the attempts recorded at or after ``turn_started_at`` for this
character in this conversation. Attempts are written inline during the
turn, so the window is sound. Scoped by ``conversation_id`` rather than
``operator_id`` — the journal doesn't carry the operator id, and a
character can be live in more than one conversation at once, so
character-only scoping would risk sweeping up another conversation's
attempts (``PersonaCuriosityRepositoryPort.delete_created_since``
enforces both).

The attempt carries a status (planned / asked / answered / ...). All of
them go — the row's existence, not its status, is the suppressor.
"""

from __future__ import annotations

from kokoro_link.application.services.turn_undo.result import UndoTally
from kokoro_link.application.services.turn_undo.step import (
    UndoContext, UndoStep,
)


class PersonaCuriosityDeleteStep(UndoStep):
    name = "persona-curiosity"

    async def apply(self, context: UndoContext, tally: UndoTally) -> None:
        repository = context.deps.persona_curiosity
        if repository is None:
            return
        journal = context.journal
        tally.deleted_curiosity_attempts = await repository.delete_created_since(
            journal.character_id, journal.conversation_id, journal.turn_started_at,
        )
