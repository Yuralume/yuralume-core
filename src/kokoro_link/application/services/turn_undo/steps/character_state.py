"""Put the character's flat state columns back."""

from __future__ import annotations

from kokoro_link.application.services.turn_snapshot_codec import (
    state_from_dict,
)
from kokoro_link.application.services.turn_undo.result import UndoTally
from kokoro_link.application.services.turn_undo.step import (
    UndoContext, UndoStep,
)


class CharacterStateRestoreStep(UndoStep):
    """Write the pre-turn ``CharacterState`` back verbatim.

    This restores the *stored* numbers. It does not on its own restore
    what a reader sees: the projection treats un-applied emotion events
    as authoritative, so the turn's deltas reappear on the next read
    unless the emotion step has already removed them. That dependency is
    expressed as ordering in the registry, not as a call from here.
    """

    name = "character-state"

    async def apply(self, context: UndoContext, tally: UndoTally) -> None:
        journal = context.journal
        if not journal.prev_character_state:
            return
        character = await context.deps.characters.get(journal.character_id)
        if character is None:
            return
        restored = state_from_dict(journal.prev_character_state)
        await context.deps.characters.save(character.with_state(restored))
        tally.restored_character_state = True
