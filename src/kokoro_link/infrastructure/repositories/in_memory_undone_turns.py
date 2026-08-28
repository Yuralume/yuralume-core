"""In-memory ``UndoneTurnRepositoryPort`` adapter.

Used by unit tests and the fake-provider dev path. Single-threaded
asyncio, so a plain dict keyed by ``turn_record_id`` is enough — and the
dict is also what makes :meth:`record` idempotent for free.
"""

from __future__ import annotations

from datetime import datetime

from kokoro_link.contracts.undone_turn import UndoneTurnRepositoryPort
from kokoro_link.domain.entities.undone_turn import UndoneTurn


class InMemoryUndoneTurnRepository(UndoneTurnRepositoryPort):
    def __init__(self) -> None:
        self._rows: dict[str, UndoneTurn] = {}

    async def record(self, tombstone: UndoneTurn) -> None:
        # First write wins: re-undoing must not move ``undone_at``
        # forward and extend the row's life past its GC window.
        self._rows.setdefault(tombstone.turn_record_id, tombstone)

    async def is_undone(self, turn_record_id: str) -> bool:
        return turn_record_id in self._rows

    async def prune(self, *, older_than: datetime) -> int:
        stale = [
            key for key, row in self._rows.items()
            if row.undone_at < older_than
        ]
        for key in stale:
            self._rows.pop(key, None)
        return len(stale)

    async def delete_for_conversation(self, conversation_id: str) -> int:
        stale = [
            key for key, row in self._rows.items()
            if row.conversation_id == conversation_id
        ]
        for key in stale:
            self._rows.pop(key, None)
        return len(stale)
