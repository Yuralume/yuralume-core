"""TU6 — remove an arc the reverted turn itself created.

``ArcRestoreStep`` can only put back an arc that existed before the
turn. When the turn *created* the character's first arc there is no
snapshot to write, and the old code gave up there — with a comment
admitting the reason was that ``prev_active_arc is None`` could not tell
"no arc existed" apart from "we never managed to look".

TU1 removed that excuse: ``journal.had_active_arc`` is tri-state, and
this step acts **only** on ``had_active_arc is False`` — the one value
that actually proves no arc existed pre-turn. ``None`` means the
snapshot was never taken (old journal, or the arc read raised), and
deleting a character's arc on a guess is far worse than declining to;
``True`` is ``ArcRestoreStep``'s case and this step stays out of it.

A ``created_at`` guard backs the flag up: if the character's current
active arc predates ``turn_started_at``, something raced (a concurrent
turn created an arc between this journal's pre-turn read and now) and
the arc is not this turn's to delete, flag notwithstanding.

An arc created this turn takes its beats with it (``StoryArc.save`` /
``delete`` operate on the whole aggregate), and the story events
realising those beats are ``StoryEventDeleteStep``'s business — that
step doesn't need this one's arc to still exist (it deletes by
``character_id`` + time window on the ``story_events`` table itself),
so the two can run in either order.
"""

from __future__ import annotations

from kokoro_link.application.services.turn_undo.result import UndoTally
from kokoro_link.application.services.turn_undo.step import (
    UndoContext, UndoStep,
)


class CreatedArcDeleteStep(UndoStep):
    name = "created-arc"

    async def apply(self, context: UndoContext, tally: UndoTally) -> None:
        repository = context.deps.arcs
        journal = context.journal
        if repository is None or journal.had_active_arc is not False:
            return
        current = await repository.get_active_for_character(journal.character_id)
        if current is None:
            return
        if current.created_at < journal.turn_started_at:
            return
        await repository.delete(current.id)
        tally.deleted_created_arc = True
