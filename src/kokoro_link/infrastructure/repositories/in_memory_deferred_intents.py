"""In-process deferred-intent store for dev / tests (HUMANIZATION_ROADMAP §3.4)."""

from __future__ import annotations

from datetime import datetime

from kokoro_link.contracts.deferred_intent import DeferredIntentRepositoryPort
from kokoro_link.domain.entities.deferred_intent import (
    STATUS_ACTIVE,
    DeferredIntent,
)


class InMemoryDeferredIntentRepository(DeferredIntentRepositoryPort):
    def __init__(self) -> None:
        self._rows: list[DeferredIntent] = []

    async def add(self, intent: DeferredIntent) -> DeferredIntent:
        self._rows.append(intent)
        return intent

    async def list_active_for(
        self,
        character_id: str,
        operator_id: str,
        *,
        now: datetime,
        limit: int = 5,
    ) -> list[DeferredIntent]:
        matches = [
            row for row in self._rows
            if row.character_id == character_id
            and row.operator_id == operator_id
            and row.is_active_at(now)
        ]
        matches.sort(key=lambda r: r.created_at, reverse=True)
        return matches[: max(1, limit)]

    async def mark_consumed(
        self, intent_id: str, *, now: datetime,
    ) -> bool:
        for idx, row in enumerate(self._rows):
            if row.id == intent_id and row.status == STATUS_ACTIVE:
                self._rows[idx] = row.marked_consumed(now=now)
                return True
        return False

    async def gc_expired(self, *, now: datetime) -> int:
        swept = 0
        for idx, row in enumerate(self._rows):
            if row.status == STATUS_ACTIVE and row.expires_at <= now:
                self._rows[idx] = row.marked_expired()
                swept += 1
        return swept

    # ---- test helpers (not part of the port) -----------------------

    def snapshot(self) -> list[DeferredIntent]:
        return list(self._rows)
