"""In-memory twin of the visible-output slot ledger (P3-Dedup parity)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from kokoro_link.contracts.visible_slots import (
    DEFAULT_SLOT_RETENTION_DAYS,
    VisibleSlotPort,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class InMemoryVisibleSlotRepository(VisibleSlotPort):
    """Parity twin: first claim of a ``(character_id, kind, slot)`` triple
    wins; every later claim of the same triple loses until it prunes out."""

    def __init__(self) -> None:
        # {(character_id, kind, slot): created_at}
        self._claims: dict[tuple[str, str, str], datetime] = {}

    async def claim(
        self, character_id: str, kind: str, slot: str,
    ) -> bool:
        key = (character_id, kind, slot)
        if key in self._claims:
            return False
        self._claims[key] = _utcnow()
        return True

    async def release(
        self, character_id: str, kind: str, slot: str,
    ) -> bool:
        return self._claims.pop((character_id, kind, slot), None) is not None

    async def prune(
        self,
        *,
        now: datetime,
        retention_days: int = DEFAULT_SLOT_RETENTION_DAYS,
    ) -> int:
        cutoff = now - timedelta(days=retention_days)
        stale = [
            key for key, created in self._claims.items() if created < cutoff
        ]
        for key in stale:
            del self._claims[key]
        return len(stale)
