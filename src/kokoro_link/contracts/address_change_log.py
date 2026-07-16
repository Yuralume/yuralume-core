"""Repository port for the per-pair address-change audit log."""

from __future__ import annotations

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
