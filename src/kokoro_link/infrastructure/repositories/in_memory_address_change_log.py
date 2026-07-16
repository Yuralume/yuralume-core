"""In-memory address-change log — used by tests and single-process runs."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import datetime, timezone

from kokoro_link.contracts.address_change_log import AddressChangeLogRepositoryPort
from kokoro_link.domain.value_objects.address_change_event import AddressChangeEvent


class InMemoryAddressChangeLogRepository(AddressChangeLogRepositoryPort):
    def __init__(self) -> None:
        self._events: list[AddressChangeEvent] = []

    async def record(self, event: AddressChangeEvent) -> AddressChangeEvent:
        now = datetime.now(timezone.utc)
        created_at = event.created_at or now
        stamped = replace(
            event,
            id=event.id or uuid.uuid4().hex,
            created_at=created_at,
            effective_at=event.effective_at or created_at,
        )
        self._events.append(stamped)
        return stamped

    async def latest(
        self, *, character_id: str, operator_id: str, direction: str,
    ) -> AddressChangeEvent | None:
        matches = [
            e
            for e in self._events
            if e.character_id == character_id
            and e.operator_id == operator_id
            and e.direction == direction
        ]
        if not matches:
            return None
        return max(matches, key=_sort_key)

    async def list_for_pair(
        self, *, character_id: str, operator_id: str,
    ) -> list[AddressChangeEvent]:
        matches = [
            e
            for e in self._events
            if e.character_id == character_id and e.operator_id == operator_id
        ]
        return sorted(matches, key=_sort_key, reverse=True)


def _sort_key(event: AddressChangeEvent) -> datetime:
    return event.effective_at or event.created_at or datetime.min.replace(
        tzinfo=timezone.utc,
    )
