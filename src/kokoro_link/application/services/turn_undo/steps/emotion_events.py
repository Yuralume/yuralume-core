"""Delete the emotion events the turn wrote.

Why this one is not cosmetic: events are written with
``applied_to_state=False``, and once any event exists the post-turn
stops writing the numbers to the flat columns at all
(``_apply_state_suggestion_compat`` keeps only ``current_intent``). The
projection is therefore the *only* place the turn's affection / trust /
fatigue / energy live. Leaving the rows behind does not merely blur the
restore — it cancels it: the character-state step two positions later
writes the pre-turn numbers back, and the very next read folds the
reverted turn's deltas onto them again and keeps doing so, decaying, for
the rest of the 24-hour window.

The memoir timeline is the visible half of the same rows. Its summary
falls back to the evidence quote, which on the ``state_suggestion``
mirror path is the first ~120 characters of the assistant reply — so a
line the player deleted stays on display until the event goes with it.

Ordering: this runs **before** ``CharacterStateRestoreStep``, so the
restored numbers survive by construction rather than by luck.
"""

from __future__ import annotations

from kokoro_link.application.services.turn_undo.result import UndoTally
from kokoro_link.application.services.turn_undo.step import (
    UndoContext, UndoStep,
)
from kokoro_link.domain.entities.emotion_event import CAUSE_TURN


class EmotionEventDeleteStep(UndoStep):
    """Removes exactly the rows this turn caused, by cause reference.

    Not by time window. The post-turn that writes these runs in the
    background — embedded as a task, hosted as a worker job — so a
    window anchored on ``turn_started_at`` races the writer in both
    directions. The turn record id is the anchor the writer already
    stamped into every row (``cause_ref_kind=turn``,
    ``cause_ref_id=turn_record_id``), so undo reads back the same key
    and the delete is exact whenever it happens to run.

    A journal with no ``turn_record_id`` is the busy-defer case: that
    branch never runs a post-turn, so there is nothing it could have
    written and nothing to key on. Skip quietly.
    """

    name = "emotion-events"

    async def apply(self, context: UndoContext, tally: UndoTally) -> None:
        repository = context.deps.emotion_events
        if repository is None:
            return
        journal = context.journal
        turn_record_id = journal.turn_record_id
        if not turn_record_id:
            return
        tally.deleted_emotion_events = await repository.delete_by_cause(
            character_id=journal.character_id,
            cause_ref_kind=CAUSE_TURN,
            cause_ref_id=turn_record_id,
        )
