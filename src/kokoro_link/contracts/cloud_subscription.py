"""Persistence contract for authoritative Cloud tenant subscription locks."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from kokoro_link.domain.entities.cloud_subscription import CloudSubscriptionState


class CloudSubscriptionRepositoryPort(Protocol):
    async def get(self, tenant_id: str) -> CloudSubscriptionState | None:
        """Return the tenant state, or None before the first projection."""

    async def set_locked(
        self,
        tenant_id: str,
        *,
        locked: bool,
        updated_at: datetime | None = None,
    ) -> CloudSubscriptionState:
        """Atomically persist the desired lock state (idempotent upsert)."""
