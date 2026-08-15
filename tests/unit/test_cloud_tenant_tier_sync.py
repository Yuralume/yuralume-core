"""Cloud->Core tenant-tier bulk projection (repo + service).

Covers the ``set_cloud_tenant_tier_for_cloud_tenant`` port method on the
in-memory repo (only cloud operators of the target tenant are retiered, others
untouched, returns the row count, and a later resolve sees the new tier) plus
the ``CloudTenantTierSyncService`` wrapper counts.
"""

from __future__ import annotations

import pytest

from kokoro_link.application.services.account_runtime_profile import (
    AccountRuntimeProfileResolver,
)
from kokoro_link.application.services.cloud_tenant_tier_sync_service import (
    CloudTenantTierSyncService,
)
from kokoro_link.domain.entities.operator_profile import OperatorProfile
from kokoro_link.domain.value_objects.account_runtime_profile import (
    AccountRuntimeProfile,
)
from kokoro_link.infrastructure.repositories.in_memory_operator_profile import (
    InMemoryOperatorProfileRepository,
)


def _cloud_operator(op_id: str, tenant: str, tier: str) -> OperatorProfile:
    return OperatorProfile(
        id=op_id,
        display_name=op_id,
        cloud_account_id=f"acct-{op_id}",
        cloud_tenant_id=tenant,
        cloud_tenant_tier=tier,
        auth_provider="cloud",
    )


async def _seed(repo) -> None:
    await repo.save(_cloud_operator("a1", "tenant-A", "basic"))
    await repo.save(_cloud_operator("a2", "tenant-A", "basic"))
    await repo.save(_cloud_operator("b1", "tenant-B", "basic"))
    # A local operator that happens to share tenant-A's key must NOT be touched.
    await repo.save(
        OperatorProfile(
            id="local-1",
            display_name="Local",
            cloud_tenant_id="tenant-A",
            cloud_tenant_tier="basic",
            auth_provider="local",
        )
    )


@pytest.mark.asyncio
async def test_bulk_update_only_touches_target_tenant_cloud_operators() -> None:
    repo = InMemoryOperatorProfileRepository()
    await _seed(repo)

    updated = await repo.set_cloud_tenant_tier_for_cloud_tenant("tenant-A", "plus")

    assert updated == 2  # a1 + a2, not b1 and not the local operator
    assert (await repo.get("a1")).cloud_tenant_tier == "plus"
    assert (await repo.get("a2")).cloud_tenant_tier == "plus"
    assert (await repo.get("b1")).cloud_tenant_tier == "basic"
    assert (await repo.get("local-1")).cloud_tenant_tier == "basic"


class _EchoTierPort:
    """Returns a profile named after whatever tier it was asked for."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def fetch(self, tier: str) -> AccountRuntimeProfile:
        self.calls.append(tier)
        return AccountRuntimeProfile(name=tier)


@pytest.mark.asyncio
async def test_bulk_update_normalises_tier_and_resolver_sees_it() -> None:
    repo = InMemoryOperatorProfileRepository()
    await repo.save(_cloud_operator("a1", "tenant-A", "basic"))
    port = _EchoTierPort()
    resolver = AccountRuntimeProfileResolver(repo, tier_profile_port=port)

    assert (await resolver.resolve_for_operator("a1")).name == "basic"

    updated = await repo.set_cloud_tenant_tier_for_cloud_tenant("tenant-A", " Plus ")

    assert updated == 1
    assert (await repo.get("a1")).cloud_tenant_tier == "plus"  # normalised
    # The resolver asks the control-plane for the *normalised* tier, so a
    # trailing-space write can never miss its profile.
    assert (await resolver.resolve_for_operator("a1")).name == "plus"
    assert port.calls == ["basic", "plus"]


@pytest.mark.asyncio
async def test_bulk_update_blank_tenant_or_tier_is_noop() -> None:
    repo = InMemoryOperatorProfileRepository()
    await repo.save(_cloud_operator("a1", "tenant-A", "basic"))

    assert await repo.set_cloud_tenant_tier_for_cloud_tenant("   ", "plus") == 0
    assert await repo.set_cloud_tenant_tier_for_cloud_tenant("tenant-A", "  ") == 0
    assert (await repo.get("a1")).cloud_tenant_tier == "basic"


@pytest.mark.asyncio
async def test_service_reports_operators_and_updated_counts() -> None:
    repo = InMemoryOperatorProfileRepository()
    await _seed(repo)
    service = CloudTenantTierSyncService(operator_profile_repository=repo)

    result = await service.apply_tier("tenant-A", "plus")

    # updated = cloud rows written (a1, a2); operators = fan-out breadth under
    # the tenant (a1, a2, local-1 — the local row is counted but not retiered).
    assert result.updated == 2
    assert result.operators == 3
    assert (await repo.get("a1")).cloud_tenant_tier == "plus"


@pytest.mark.asyncio
async def test_service_unknown_tenant_returns_zeroes() -> None:
    repo = InMemoryOperatorProfileRepository()
    service = CloudTenantTierSyncService(operator_profile_repository=repo)

    result = await service.apply_tier("ghost-tenant", "plus")

    assert result.updated == 0
    assert result.operators == 0
