"""Undone-turn tombstone port.

Storage-agnostic access to the ``undone_turns`` table. Two consumers,
pulling in opposite directions:

* the **undo** writes one row per reversed turn (:meth:`record`),
* the **post-turn** reads it before writing anything (:meth:`is_undone`)
  — in embedded mode from the same process, in hosted from a worker.

:meth:`is_undone` is on the hot path of every turn, so it must stay a
single primary-key lookup; that is why the tombstone is keyed by
``turn_record_id`` and carries no other query surface.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from kokoro_link.domain.entities.undone_turn import UndoneTurn


class UndoneTurnRepositoryPort(Protocol):
    async def record(self, tombstone: UndoneTurn) -> None:
        """Mark a turn as reversed. Idempotent — recording the same
        ``turn_record_id`` twice keeps the first ``undone_at`` and must
        not raise."""

    async def is_undone(self, turn_record_id: str) -> bool:
        """Has this turn been reversed? The post-turn gate's whole
        question."""

    async def prune(self, *, older_than: datetime) -> int:
        """Drop tombstones stamped before ``older_than``; return the
        count removed.

        The retention window has to outlast the slowest post-turn that
        could still be in flight — dropping a tombstone early re-opens
        exactly the race it exists to close, so the caller picks a
        window with margin rather than the tightest one that fits.
        """

    async def delete_for_conversation(self, conversation_id: str) -> int:
        """Cascade-delete every tombstone for the conversation.

        Only the in-memory adapter needs this: the SQL table carries an
        ``ON DELETE CASCADE`` foreign key to ``conversations`` and gets
        it from the database.
        """
