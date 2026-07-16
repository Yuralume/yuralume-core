"""Ports for account runtime policy usage events."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


ACCOUNT_RUNTIME_EVENT_CHARACTER_CREATE = "character_create"
ACCOUNT_RUNTIME_EVENT_CHAT_IMAGE = "chat_image"
ACCOUNT_RUNTIME_EVENT_FEED_POST = "feed_post"


@dataclass(frozen=True, slots=True)
class AccountRuntimeUsageEvent:
    operator_id: str
    event_type: str
    occurred_at: datetime
    resource_id: str | None = None


class AccountRuntimeUsageRepositoryPort(Protocol):
    """Record and query account-level events that drive runtime limits."""

    async def record_event(
        self,
        *,
        operator_id: str,
        event_type: str,
        occurred_at: datetime,
        resource_id: str | None = None,
    ) -> None: ...

    async def count_events(
        self,
        *,
        operator_id: str,
        event_type: str,
        since: datetime,
        until: datetime | None = None,
    ) -> int: ...

    async def list_events(
        self,
        *,
        event_type: str,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[AccountRuntimeUsageEvent]: ...
