from __future__ import annotations

from hashlib import sha256

from kokoro_link.contracts.cloud_gateway import (
    CloudGatewayIdentity,
    CloudIdentityUnavailable,
    CloudResourceContext,
)
from kokoro_link.contracts.operator_profile import OperatorProfileRepositoryPort
from kokoro_link.domain.entities.operator_profile import OperatorProfile
from kokoro_link.application.services.subscription_access_guard import (
    SubscriptionAccessGuard,
)


class CloudOperatorIdentityResolver:
    def __init__(
        self,
        *,
        repository: OperatorProfileRepositoryPort,
        subscription_access_guard: SubscriptionAccessGuard | None = None,
    ) -> None:
        self._repository = repository
        self._subscription_access_guard = subscription_access_guard

    async def resolve_context(
        self, context: CloudResourceContext
    ) -> CloudGatewayIdentity:
        operator_key = (context.operator_id or "").strip()
        if not operator_key:
            raise CloudIdentityUnavailable("cloud operator id is empty")
        operator = await self._repository.get(operator_key)
        if operator is None:
            raise CloudIdentityUnavailable(
                f"cloud operator projection not found: {operator_key}",
            )
        account_id, tenant_id = _cloud_ids(operator)
        if self._subscription_access_guard is not None:
            await self._subscription_access_guard.ensure_tenant_allowed(tenant_id)
        character_ref = ""
        if context.character is not None:
            character_ref = _character_ref(
                tenant_id=tenant_id,
                account_id=account_id,
                character_id=context.character.id,
            )
        return CloudGatewayIdentity(
            operator_id=operator.id,
            account_id=account_id,
            tenant_id=tenant_id,
            character_ref=character_ref,
            tenant_tier=(operator.cloud_tenant_tier or "standard").strip() or "standard",
        )


def _cloud_ids(operator: OperatorProfile) -> tuple[str, str]:
    if operator.auth_provider != "cloud":
        raise CloudIdentityUnavailable(
            f"operator {operator.id} is not a cloud projection",
        )
    account_id = (operator.cloud_account_id or "").strip()
    tenant_id = (operator.cloud_tenant_id or "").strip()
    if not account_id or not tenant_id:
        raise CloudIdentityUnavailable(
            f"operator {operator.id} is missing cloud account/tenant ids",
        )
    return account_id, tenant_id


def _character_ref(
    *,
    tenant_id: str,
    account_id: str,
    character_id: str,
) -> str:
    digest = sha256(
        f"{tenant_id}:{account_id}:{character_id}".encode("utf-8"),
    ).hexdigest()
    return f"chr_{digest[:32]}"
