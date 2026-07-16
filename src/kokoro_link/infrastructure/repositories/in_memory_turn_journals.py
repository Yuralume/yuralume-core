"""In-memory ``TurnJournalRepositoryPort`` adapter.

Used by unit tests and the fake-provider dev path. Thread-safety isn't
a concern because the codebase is asyncio single-threaded.
"""

from __future__ import annotations

from kokoro_link.contracts.turn_journal import TurnJournalRepositoryPort
from kokoro_link.domain.entities.turn_journal import TurnJournal


class InMemoryTurnJournalRepository(TurnJournalRepositoryPort):
    def __init__(self) -> None:
        self._rows: dict[str, TurnJournal] = {}

    async def add(self, journal: TurnJournal) -> None:
        self._rows[journal.id] = journal

    async def save(self, journal: TurnJournal) -> None:
        self._rows[journal.id] = journal

    async def get_latest(self, conversation_id: str) -> TurnJournal | None:
        candidates = [
            j for j in self._rows.values()
            if j.conversation_id == conversation_id
        ]
        if not candidates:
            return None
        candidates.sort(
            key=lambda j: (j.turn_index, j.created_at), reverse=True,
        )
        return candidates[0]

    async def list_for_conversation(
        self, conversation_id: str, *, limit: int = 5,
    ) -> list[TurnJournal]:
        candidates = [
            j for j in self._rows.values()
            if j.conversation_id == conversation_id
        ]
        candidates.sort(
            key=lambda j: (j.turn_index, j.created_at), reverse=True,
        )
        return candidates[: max(0, limit)]

    async def delete(self, journal_id: str) -> bool:
        return self._rows.pop(journal_id, None) is not None

    async def prune_for_conversation(
        self, conversation_id: str, *, keep: int = 5,
    ) -> int:
        candidates = sorted(
            (j for j in self._rows.values() if j.conversation_id == conversation_id),
            key=lambda j: (j.turn_index, j.created_at),
            reverse=True,
        )
        if len(candidates) <= keep:
            return 0
        stale = candidates[keep:]
        for item in stale:
            self._rows.pop(item.id, None)
        return len(stale)

    async def delete_for_conversation(self, conversation_id: str) -> int:
        ids = [
            j.id for j in self._rows.values()
            if j.conversation_id == conversation_id
        ]
        for jid in ids:
            self._rows.pop(jid, None)
        return len(ids)

    async def delete_for_character(self, character_id: str) -> int:
        ids = [
            j.id for j in self._rows.values()
            if j.character_id == character_id
        ]
        for jid in ids:
            self._rows.pop(jid, None)
        return len(ids)
