"""UndoneTurn — the tombstone an undo leaves behind.

A turn's post-turn extraction (memories, emotion events, promises, state
suggestion) runs *after* the reply is handed to the player: a
fire-and-forget task in embedded mode, a queued worker job in hosted. An
undo can therefore land while that work is still in flight, and nothing
in the rollback can stop it — by the time the extraction writes, the
deletes it was supposed to undo have already run, so the turn comes back
from the dead one subsystem at a time.

This row is the interlock. It says nothing about *how* a turn was
reversed, only that ``turn_record_id`` must never be written for again.
Three properties are what make it work, and each is a design choice
rather than an incidental detail:

* **Its own table, not a flag on the journal.** The last thing an undo
  does is delete the journal row. A tombstone stored there would die
  with the record it exists to outlive.
* **Keyed by ``turn_record_id``.** The gate is checked by a background
  process holding an id and nothing else. A marker buried inside the
  journal's ``payload_json`` blob cannot be looked up by that id.
* **Durable, not in-process.** Hosted runs the post-turn in a different
  process from the API that served the undo, so "wait for the in-flight
  task" is unreachable there. A row in the database is not.

``turn_record_id`` is the primary key, which makes recording idempotent:
undoing the same turn twice is one tombstone, not a duplicate-key crash.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class UndoneTurn:
    turn_record_id: str
    """The ``turn_records`` id of the reversed turn — the only key the
    post-turn gate has to ask with."""
    conversation_id: str
    """Kept for cascade cleanup and for making a stray tombstone
    diagnosable; never part of the gate's lookup."""
    undone_at: datetime
    """When the undo ran. The GC sweep's only input — a tombstone is
    worthless once no post-turn for that turn could still be in flight."""

    def __post_init__(self) -> None:
        if not self.turn_record_id:
            raise ValueError("UndoneTurn.turn_record_id is required")
        if not self.conversation_id:
            raise ValueError("UndoneTurn.conversation_id is required")
        if self.undone_at.tzinfo is None:
            object.__setattr__(
                self, "undone_at", self.undone_at.replace(tzinfo=timezone.utc),
            )

    @classmethod
    def new(
        cls,
        *,
        turn_record_id: str,
        conversation_id: str,
        undone_at: datetime | None = None,
    ) -> "UndoneTurn":
        return cls(
            turn_record_id=turn_record_id,
            conversation_id=conversation_id,
            undone_at=undone_at or _utcnow(),
        )
