"""In-memory Cloud tenant subscription state repository."""

from __future__ import annotations

from datetime import datetime, timezone

from kokoro_link.contracts.cloud_subscription import (
    CloudSubscriptionRepositoryPort,
)
from kokoro_link.domain.entities.cloud_subscription import CloudSubscriptionState


class InMemoryCloudSubscriptionRepository(CloudSubscriptionRepositoryPort):
    def __init__(self) -> None:
        self._states: dict[str, CloudSubscriptionState] = {}

    async def get(self, tenant_id: str) -> CloudSubscriptionState | None:
        return self._states.get(_normalise_tenant_id(tenant_id))

    async def set_locked(
        self,
        tenant_id: str,
        *,
        locked: bool,
        updated_at: datetime | None = None,
    ) -> CloudSubscriptionState:
        state = CloudSubscriptionState(
            tenant_id=_normalise_tenant_id(tenant_id),
            locked=bool(locked),
            updated_at=updated_at or datetime.now(timezone.utc),
        )
        self._states[state.tenant_id] = state
        return state


def _normalise_tenant_id(tenant_id: str) -> str:
    value = (tenant_id or "").strip()
    if not value:
        raise ValueError("tenant_id must be non-empty")
    return value
