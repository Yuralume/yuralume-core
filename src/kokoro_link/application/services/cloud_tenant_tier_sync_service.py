"""Cloud->Core tenant-tier sync.

The operator's ``cloud_tenant_tier`` projection is otherwise refreshed only at
login (``CloudFederatedAuthStrategy``). This service applies a tier change
pushed by Yuralume Cloud immediately, so an upgrade / downgrade takes effect
without waiting for the operator to re-login — closing the staleness window
that (for example) lets the demo reaper delete a freshly-upgraded tenant's
characters. The write is a single authoritative bulk projection; the caller
retries on error through its own outbox.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from kokoro_link.contracts.operator_profile import OperatorProfileRepositoryPort

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CloudTenantTierSyncResult:
    """Outcome of one tenant tier push.

    ``operators`` is how many operator rows the tenant fans out to (breadth);
    ``updated`` is how many cloud operator rows had their tier written."""

    operators: int = 0
    updated: int = 0


class CloudTenantTierSyncService:
    """Project a pushed subscription tier onto a Cloud tenant's operators."""

    def __init__(
        self,
        *,
        operator_profile_repository: OperatorProfileRepositoryPort,
    ) -> None:
        self._operator_profile_repository = operator_profile_repository

    async def apply_tier(
        self, tenant_id: str, tier: str,
    ) -> CloudTenantTierSyncResult:
        """Write ``tier`` to every cloud operator under ``tenant_id``.

        Idempotent — re-applying the same tier is a no-op in effect. Repository
        errors bubble up so the internal route surfaces a 500 and the Cloud
        caller retries."""
        updated = await (
            self._operator_profile_repository.set_cloud_tenant_tier_for_cloud_tenant(
                tenant_id, tier,
            )
        )
        operators = len(
            await self._operator_profile_repository.list_by_cloud_tenant_id(
                tenant_id,
            )
        )
        _LOGGER.info(
            "cloud tenant tier sync: tenant=%s tier=%s operators=%d updated=%d",
            tenant_id, tier, operators, updated,
        )
        return CloudTenantTierSyncResult(operators=operators, updated=updated)
