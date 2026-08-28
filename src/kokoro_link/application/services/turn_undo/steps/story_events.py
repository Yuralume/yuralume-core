"""TU6 — un-realise the arc beat the reverted turn played.

``TurnUndoService`` used to excuse leaving ``story_events`` alone on the
grounds that they regenerate daily via ``ensure_today``. That is true of
the gacha-rolled daily events (``seed_id`` set) and false of the one
that matters: an arc beat realisation (``arc_beat_id`` set) is a
one-shot record of a beat having been played, and nothing regenerates
it. Undo the turn and the beat stays spent unless this step runs.

Scoped to arc-beat realisations (``arc_beat_id IS NOT NULL``) created
at or after ``turn_started_at``; the daily generated events genuinely
are disposable and must not be swept up
(``delete_arc_beat_realizations_since`` enforces both filters).

This step does **not** reach into the arc to clear the beat's
``realized_event_id`` — the arc side of an undone realisation is
already handled by whichever arc step ran earlier in the registry:
``ArcRestoreStep`` overwrites the whole pre-turn arc (beat back to
``pending``, no ``realized_event_id``) when one existed, and
``CreatedArcDeleteStep`` deletes the arc — beats included — when the
turn created it. Either way the beat's pointer is gone with the beat,
not left dangling; a ``story_events`` row surviving between those two
steps and this one is exactly the acceptable orphan the row's own
docstring already allows (``realized_event_id`` is not a formal FK).
"""

from __future__ import annotations

from kokoro_link.application.services.turn_undo.result import UndoTally
from kokoro_link.application.services.turn_undo.step import (
    UndoContext, UndoStep,
)


class StoryEventDeleteStep(UndoStep):
    name = "story-events"

    async def apply(self, context: UndoContext, tally: UndoTally) -> None:
        repository = context.deps.story_events
        if repository is None:
            return
        journal = context.journal
        tally.deleted_story_events = (
            await repository.delete_arc_beat_realizations_since(
                journal.character_id, journal.turn_started_at,
            )
        )
