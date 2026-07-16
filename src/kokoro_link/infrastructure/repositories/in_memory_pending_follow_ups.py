"""In-memory ``PendingFollowUpRepositoryPort`` adapter.

Used by unit tests and the fake-provider dev path. Asyncio single-
threaded — no locking needed.
"""

from __future__ import annotations

from datetime import datetime

from kokoro_link.contracts.pending_follow_up import (
    PendingFollowUpRepositoryPort,
)
from kokoro_link.domain.entities.pending_follow_up import (
    PendingFollowUp,
    PendingFollowUpStatus,
)


_OPEN_STATUSES: frozenset[str] = frozenset({
    PendingFollowUpStatus.QUEUED.value,
    PendingFollowUpStatus.RESOLVING.value,
})


class InMemoryPendingFollowUpRepository(PendingFollowUpRepositoryPort):
    def __init__(self) -> None:
        self._rows: dict[str, PendingFollowUp] = {}

    async def add(self, follow_up: PendingFollowUp) -> None:
        self._rows[follow_up.id] = follow_up

    async def save(self, follow_up: PendingFollowUp) -> None:
        self._rows[follow_up.id] = follow_up

    async def get(self, follow_up_id: str) -> PendingFollowUp | None:
        return self._rows.get(follow_up_id)

    async def find_open_for_conversation(
        self, conversation_id: str,
    ) -> PendingFollowUp | None:
        candidates = [
            row for row in self._rows.values()
            if row.conversation_id == conversation_id
            and row.status.value in _OPEN_STATUSES
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda row: row.queued_at, reverse=True)
        return candidates[0]

    async def list_due(
        self,
        *,
        now: datetime,
        limit: int = 50,
    ) -> list[PendingFollowUp]:
        eligible = [
            row for row in self._rows.values()
            if row.status == PendingFollowUpStatus.QUEUED
            and row.scheduled_for <= now
        ]
        eligible.sort(key=lambda row: row.scheduled_for)
        return eligible[: max(0, limit)]

    async def list_open_for_character(
        self, character_id: str,
    ) -> list[PendingFollowUp]:
        return [
            row for row in self._rows.values()
            if row.character_id == character_id
            and row.status.value in _OPEN_STATUSES
        ]

    async def delete_for_conversation(self, conversation_id: str) -> int:
        ids = [
            row.id for row in self._rows.values()
            if row.conversation_id == conversation_id
        ]
        for rid in ids:
            self._rows.pop(rid, None)
        return len(ids)

    async def delete_for_character(self, character_id: str) -> int:
        ids = [
            row.id for row in self._rows.values()
            if row.character_id == character_id
        ]
        for rid in ids:
            self._rows.pop(rid, None)
        return len(ids)
