"""TU5 — re-open a 起幕 scene the turn closed.

A turn can be judged to have resolved the scene (``_close_scene_if_resolved``),
which closes the session. Nothing in the player's hands re-opens a closed
scene, so an undo that leaves the close standing has made a one-way door
out of a reversible action.

``journal.prev_scene_session`` holds the pre-turn row verbatim — captured
while it was still ``open``, before this turn could touch it — so
``scene_session_from_dict`` decodes straight back into a valid ``open``
entity with no ``closed_at`` / ``closed_reason`` to strip. The restore is
a ``save`` of that decoded entity, gated on **this turn** having been
what closed the scene. Two independent facts have to agree before the
reopen fires, because "the row is closed now" is not one of them:

* **``closed_reason`` is ``resolved``.** Three callers can close a
  session and only one of them is the turn: the in-turn verdict
  (``resolved``), the player's own 「結束場景」 button (``manual``), and
  SC1-E's idle sweep (``timeout``). The last two are the player's own
  decision and the world moving on — reopening either would hand back a
  scene nobody asked to re-enter, clear its ``closed_at`` /
  ``closed_reason``, rewind ``last_activity_at``, and then tell the
  player ``restored_scene_session=True`` about it.
* **``closed_at`` is at or after ``journal.turn_started_at``.** A
  ``resolved`` close is the right *kind* of close but not necessarily
  *this turn's*; the timestamp is what binds it to the window the
  journal describes. A close that predates the turn cannot be its doing
  whatever its reason says.

An ordinary in-scene turn leaves the session open and only bumps
``last_activity_at`` (monotonic); blindly re-saving the pre-turn
snapshot over that would drag the timestamp backwards and make a live
scene look idle to the timeout closer, so the open case returns before
either check.

The closing narration is a ``Message`` appended to the conversation during
the turn, so ``ConversationTruncateStep`` (which runs earlier in the
registry) has already dropped it along with the rest of the turn's tail —
nothing here needs to touch the thread.

The storage layer enforces **at most one open session per character** and
raises ``SceneSessionConflict`` rather than trusting the caller. If a
*different* scene has opened since (its own id, not this session's), the
conflict means re-opening this one is not possible; that failure is
caught and reported as ``restored_scene_session=False`` instead of
failing the whole undo.
"""

from __future__ import annotations

import logging
from datetime import datetime

from kokoro_link.application.services.turn_journal_snapshots import (
    scene_session_from_dict,
)
from kokoro_link.application.services.turn_undo.result import UndoTally
from kokoro_link.application.services.turn_undo.step import (
    UndoContext, UndoStep,
)
from kokoro_link.contracts.clock import ensure_utc
from kokoro_link.contracts.story_scene import SceneSessionConflict
from kokoro_link.domain.entities.story_scene_session import (
    SCENE_CLOSE_RESOLVED, StorySceneSession,
)

_LOGGER = logging.getLogger(__name__)


class SceneSessionRestoreStep(UndoStep):
    name = "scene-session"

    async def apply(self, context: UndoContext, tally: UndoTally) -> None:
        repository = context.deps.scene_sessions
        if repository is None:
            return
        snapshot = context.journal.prev_scene_session
        if snapshot is None:
            return
        try:
            restored = scene_session_from_dict(snapshot)
        except Exception:
            _LOGGER.exception(
                "undo: scene session snapshot decode failed character=%s",
                context.journal.character_id,
            )
            return
        try:
            current = await repository.get(restored.id)
        except Exception:
            _LOGGER.exception(
                "undo: scene session lookup failed character=%s",
                context.journal.character_id,
            )
            return
        if current is None or current.is_open:
            # Either the row is gone, or this turn never closed it — an
            # ordinary in-scene turn only bumps ``last_activity_at``.
            # Re-saving the pre-turn snapshot in that case would drag a
            # live scene's activity clock backwards.
            return
        if not _closed_by_this_turn(current, context.journal.turn_started_at):
            # Closed, but by the player or by the idle sweep. Reopening
            # would undo somebody else's decision and report it as this
            # turn's rollback.
            _LOGGER.info(
                "undo: scene session %s left closed — reason=%s is not this "
                "turn's close (character=%s)",
                current.id, current.closed_reason,
                context.journal.character_id,
            )
            return
        try:
            await repository.save(restored)
        except SceneSessionConflict:
            _LOGGER.info(
                "undo: scene session %s could not reopen — a different "
                "scene is already open for character=%s",
                restored.id, context.journal.character_id,
            )
            return
        except Exception:
            _LOGGER.exception(
                "undo: scene session restore failed character=%s",
                context.journal.character_id,
            )
            return
        tally.restored_scene_session = True


def _closed_by_this_turn(
    current: StorySceneSession, turn_started_at: datetime,
) -> bool:
    """Was ``current`` closed by the turn the journal describes?

    Both halves fail closed. A ``closed_reason`` this code does not
    recognise as the in-turn verdict, or a missing ``closed_at`` (a row
    written before the column carried one), answers "not ours" — leaving
    a scene closed is recoverable by starting the next one, whereas
    reopening a scene the player deliberately ended is not.

    Timestamps are normalised because the journal and the session row
    travel through different stores: SQLite hands back naive values for
    a ``DateTime(timezone=True)`` column where PostgreSQL keeps the
    offset, and a naive/aware comparison raises rather than answering.
    """
    if current.closed_reason != SCENE_CLOSE_RESOLVED:
        return False
    if current.closed_at is None:
        return False
    return ensure_utc(current.closed_at) >= ensure_utc(turn_started_at)
