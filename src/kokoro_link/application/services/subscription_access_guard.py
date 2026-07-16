"""Single authoritative guard for lapsed Cloud tenant access."""

from __future__ import annotations

from kokoro_link.contracts.cloud_subscription import (
    CloudSubscriptionRepositoryPort,
)
from kokoro_link.contracts.operator_profile import OperatorProfileRepositoryPort
from kokoro_link.domain.entities.character import Character


class SubscriptionAccessLocked(RuntimeError):
    """Raised before paid work when the owning Cloud tenant is locked."""

    def __init__(self, tenant_id: str) -> None:
        self.tenant_id = tenant_id
        super().__init__("subscription is inactive; renew to continue")


class SubscriptionAccessGuard:
    """Resolve operator to Cloud tenant and enforce persisted desired state."""

    def __init__(
        self,
        *,
        subscription_repository: CloudSubscriptionRepositoryPort,
        operator_profile_repository: OperatorProfileRepositoryPort,
    ) -> None:
        self._subscriptions = subscription_repository
        self._operators = operator_profile_repository

    async def ensure_operator_allowed(self, operator_id: str) -> None:
        tenant_id = await self._cloud_tenant_for_operator(operator_id)
        if tenant_id is None:
            return
        await self.ensure_tenant_allowed(tenant_id)

    async def ensure_tenant_allowed(self, tenant_id: str) -> None:
        state = await self._subscriptions.get(tenant_id)
        if state is not None and state.locked:
            raise SubscriptionAccessLocked(tenant_id)

    async def ensure_character_allowed(self, character: Character) -> None:
        await self.ensure_operator_allowed(character.user_id)

    async def is_operator_allowed(self, operator_id: str) -> bool:
        try:
            await self.ensure_operator_allowed(operator_id)
        except SubscriptionAccessLocked:
            return False
        return True

    async def is_character_allowed(self, character: Character) -> bool:
        return await self.is_operator_allowed(character.user_id)

    async def _cloud_tenant_for_operator(self, operator_id: str) -> str | None:
        operator = await self._operators.get((operator_id or "").strip())
        if operator is None or operator.auth_provider != "cloud":
            return None
        tenant_id = (operator.cloud_tenant_id or "").strip()
        return tenant_id or None
