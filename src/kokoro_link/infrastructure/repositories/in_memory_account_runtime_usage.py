"""In-process account runtime usage ledger for tests and dev mode."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from kokoro_link.contracts.account_runtime_usage import AccountRuntimeUsageEvent
from kokoro_link.contracts.clock import ensure_utc


@dataclass(frozen=True, slots=True)
class _AccountRuntimeEvent:
    operator_id: str
    event_type: str
    occurred_at: datetime
    resource_id: str | None = None


class InMemoryAccountRuntimeUsageRepository:
    def __init__(self) -> None:
        self._events: list[_AccountRuntimeEvent] = []

    async def record_event(
        self,
        *,
        operator_id: str,
        event_type: str,
        occurred_at: datetime,
        resource_id: str | None = None,
    ) -> None:
        self._events.append(
            _AccountRuntimeEvent(
                operator_id=operator_id,
                event_type=event_type,
                occurred_at=ensure_utc(occurred_at),
                resource_id=_normalise_resource_id(resource_id),
            ),
        )

    async def count_events(
        self,
        *,
        operator_id: str,
        event_type: str,
        since: datetime,
        until: datetime | None = None,
    ) -> int:
        since_utc = ensure_utc(since)
        until_utc = ensure_utc(until) if until is not None else None
        return sum(
            1
            for event in self._events
            if event.operator_id == operator_id
            and event.event_type == event_type
            and event.occurred_at >= since_utc
            and (until_utc is None or event.occurred_at <= until_utc)
        )

    async def list_events(
        self,
        *,
        event_type: str,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[AccountRuntimeUsageEvent]:
        since_utc = ensure_utc(since) if since is not None else None
        until_utc = ensure_utc(until) if until is not None else None
        events = [
            AccountRuntimeUsageEvent(
                operator_id=event.operator_id,
                event_type=event.event_type,
                occurred_at=event.occurred_at,
                resource_id=event.resource_id,
            )
            for event in self._events
            if event.event_type == event_type
            and (since_utc is None or event.occurred_at >= since_utc)
            and (until_utc is None or event.occurred_at <= until_utc)
        ]
        return sorted(events, key=lambda event: event.occurred_at)


def _normalise_resource_id(resource_id: str | None) -> str | None:
    if resource_id is None:
        return None
    value = resource_id.strip()
    return value or None
