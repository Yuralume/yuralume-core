"""TU6 — forget a meeting the reverted turn arranged.

``character_encounter_intents`` records that two characters agreed to
meet. Undo deletes the rows *this character* recorded during the
reverted turn (``_persist_peer_meet_intents``), so the character no
longer believes the appointment was made.

Only rows this turn created go: the pending-intent table is shared by
both characters in a pair, and an intent that predates the turn — or
that the peer recorded — is not this turn's fact to delete. That
scoping lives in ``delete_by_turn_record`` (filters on ``character_id``,
never ``peer_character_id``).

**By anchor, not by time window** — the correction TU3 already made for
emotion events, needed here for two reasons rather than one.

The docstring this file used to carry claimed the opposite: "unlike the
emotion events, these are written inline during the turn rather than by
a background pass, so a time window is sound here." That premise was
false. ``_persist_peer_meet_intents`` has exactly one call site and it
sits inside ``_do_post_turn``, after ``processor.process()`` — the same
background writer the emotion events have, a fire-and-forget task
embedded and a worker job hosted. A window floored on ``turn_started_at``
therefore raced the writer in both directions: too wide when a previous
turn's post-turn landed inside this turn's window, too narrow when this
turn's own post-turn had not landed yet.

The second reason is specific to this table and worse: it has no
conversation column, so the old delete was scoped by ``character_id``
and ``created_at`` and nothing else. One character can be live in a web
conversation and a LINE conversation at the same time —
``recent_messages_for_character`` reads across sources by design — so
undoing a turn in thread A deleted a meeting thread B had just agreed
to, with nothing in thread B undone. The sibling ``persona_curiosity``
step saw that hazard and passes a ``conversation_id``; this one did not.
The anchor closes it without needing a column at all: a turn record
names one turn, and a turn belongs to one conversation.

A journal with no ``turn_record_id`` is skipped quietly, exactly as the
emotion-event step skips one: that is the busy-defer branch, which runs
no post-turn and so can have written no intent. Rows written before the
anchor column existed are left alone for the same reason — during a
rolling deployment that loses the deletion of an in-flight intent, which
costs a stale appointment the character may mention, whereas falling
back to the old window would risk deleting another conversation's real
one.
"""

from __future__ import annotations

from kokoro_link.application.services.turn_undo.result import UndoTally
from kokoro_link.application.services.turn_undo.step import (
    UndoContext, UndoStep,
)


class EncounterIntentDeleteStep(UndoStep):
    name = "encounter-intents"

    async def apply(self, context: UndoContext, tally: UndoTally) -> None:
        repository = context.deps.encounter_intents
        if repository is None:
            return
        journal = context.journal
        turn_record_id = journal.turn_record_id
        if not turn_record_id:
            return
        tally.deleted_encounter_intents = (
            await repository.delete_by_turn_record(
                journal.character_id, turn_record_id,
            )
        )
