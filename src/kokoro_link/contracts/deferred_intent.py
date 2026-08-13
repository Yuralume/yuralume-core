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

    async def list_due_for(
        self,
        character_id: str,
        operator_id: str,
        *,
        now: datetime,
        limit: int = 5,
    ) -> list[DeferredIntent]:
        """Return still-live motives whose ``revisit_at`` alarm has rung
        (``revisit_at <= now``). Newest first.

        Deliberately narrower than ``list_active_for``: the dispatcher
        runs this on the cheap side of the gate, so implementations must
        push the ``revisit_at IS NOT NULL`` filter down to the store
        rather than fetching everything and filtering in Python."""

    async def clear_revisit(self, intent_id: str) -> bool:
        """Drop the ``revisit_at`` alarm on one row, keeping the motive.

        Returns ``True`` when a row was updated. Called the moment an
        alarm is spent so it cannot exempt a second tick."""

    async def restore_revisit(
        self, intent_id: str, *, revisit_at: datetime,
    ) -> bool:
        """Undo a spend on a row whose tick never produced a judgement.

        Only applies to a row that is still ``status=active`` **and**
        currently alarm-less, so a concurrent consume — or a fresher
        appointment written by another instance — always wins over a
        late restore. Returns ``True`` when a row was updated."""

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
