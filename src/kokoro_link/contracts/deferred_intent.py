"""Repository port for ``DeferredIntent`` rows (HUMANIZATION_ROADMAP §3.4)."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from kokoro_link.domain.entities.deferred_intent import DeferredIntent


class DeferredIntentRepositoryPort(Protocol):
    async def add(self, intent: DeferredIntent) -> DeferredIntent:
        """Persist a freshly recorded deferred motive."""

    async def list_active_for(
        self,
        character_id: str,
        operator_id: str,
        *,
        now: datetime,
        limit: int = 5,
    ) -> list[DeferredIntent]:
        """Return ``status=active`` motives for the pair whose ``expires_at``
        is still after ``now``. Newest first."""

    async def mark_consumed(
        self, intent_id: str, *, now: datetime,
    ) -> bool:
        """Flip an active row to ``status=consumed``. Returns ``True``
        when a row was updated, ``False`` when missing / already consumed."""

    async def gc_expired(self, *, now: datetime) -> int:
        """Sweep ``status=active`` rows past TTL to ``status=expired``.

        Returns the number of rows updated. Best-effort housekeeping —
        callers may invoke this from a periodic tick or before each
        ``list_active_for`` if they prefer aggressive cleanup."""
