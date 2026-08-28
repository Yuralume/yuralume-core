"""Repository port for the per-pair address-change audit log."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from kokoro_link.domain.value_objects.address_change_event import AddressChangeEvent


class AddressChangeLogRepositoryPort(Protocol):
    async def record(self, event: AddressChangeEvent) -> AddressChangeEvent:
        """Persist a change event; returns it stamped with id/timestamps."""

    async def latest(
        self, *, character_id: str, operator_id: str, direction: str,
    ) -> AddressChangeEvent | None:
        """Most recent change for one pair + direction, if any."""

    async def list_for_pair(
        self, *, character_id: str, operator_id: str,
    ) -> list[AddressChangeEvent]:
        """All changes for a pair, newest first."""

    async def delete_observed_since(
        self, *, character_id: str, operator_id: str, since: datetime,
    ) -> list[AddressChangeEvent]:
        """Delete ``source="observed"`` events at/after ``since`` for the
        pair and return exactly what was deleted.

        Combined read+delete so a caller (TU5's undo step) never has to
        list then delete in two round trips that could race a concurrent
        write in between. Scoped to ``observed`` only — a ``player_edit``
        made through the settings UI is a deliberate action, not a side
        effect of the turn being undone, and must never be rolled back by
        this. The returned events carry ``old_value``, which is what the
        caller restores the seed field to."""
