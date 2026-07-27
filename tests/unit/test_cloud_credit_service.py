"""U3 — per-tenant credit-balance cache with fail-soft last-known-good.

Display-only data (HOSTED_PLAYER_UX_PLAN §4.1): a short TTL keeps the badge
"timely", and an upstream outage serves the previous value flagged ``stale``
rather than blocking play. No cross-instance consistency requirement, so no
pg_notify invalidation.
"""

from __future__ import annotations

import pytest

from kokoro_link.application.services.cloud_credit_service import (
    CloudCreditService,
)
from kokoro_link.contracts.cloud_credit_balance import (
    CloudCreditBalance,
    CreditBalanceUnavailable,
)


class _FakeClient:
    def __init__(self, *, balances: list[CloudCreditBalance | Exception]) -> None:
        self._balances = list(balances)
        self.calls: list[str] = []

    async def fetch(self, tenant_id: str) -> CloudCreditBalance:
        self.calls.append(tenant_id)
        item = (
            self._balances.pop(0)
            if len(self._balances) > 1
            else self._balances[0]
        )
        if isinstance(item, Exception):
            raise item
        return item


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _balance(total: float) -> CloudCreditBalance:
    return CloudCreditBalance(
        gift_cr=total / 2,
        purchased_cr=total / 2,
        total_cr=total,
    )


@pytest.mark.asyncio
async def test_returns_fresh_balance_from_upstream() -> None:
    client = _FakeClient(balances=[_balance(15.0)])
    service = CloudCreditService(client=client)

    snapshot = await service.get_balance("tenant-1")

    assert snapshot is not None
    assert snapshot.total_cr == pytest.approx(15.0)
    assert snapshot.gift_cr == pytest.approx(7.5)
    assert snapshot.purchased_cr == pytest.approx(7.5)
    assert snapshot.stale is False


@pytest.mark.asyncio
async def test_unknown_tenant_all_zero_is_a_real_answer() -> None:
    client = _FakeClient(balances=[_balance(0.0)])
    service = CloudCreditService(client=client)

    snapshot = await service.get_balance("ghost-tenant")

    assert snapshot is not None
    assert snapshot.total_cr == pytest.approx(0.0)
    assert snapshot.stale is False


@pytest.mark.asyncio
async def test_warm_cache_is_served_without_a_second_call() -> None:
    clock = _Clock()
    client = _FakeClient(balances=[_balance(15.0), _balance(99.0)])
    service = CloudCreditService(client=client, time_source=clock)

    first = await service.get_balance("tenant-1")
    clock.now += 5.0
    second = await service.get_balance("tenant-1")

    assert client.calls == ["tenant-1"]
    assert first == second


@pytest.mark.asyncio
async def test_expired_cache_refetches_after_ttl() -> None:
    clock = _Clock()
    client = _FakeClient(balances=[_balance(15.0), _balance(99.0)])
    service = CloudCreditService(
        client=client, ttl_seconds=20.0, time_source=clock,
    )

    await service.get_balance("tenant-1")
    clock.now += 21.0
    refreshed = await service.get_balance("tenant-1")

    assert client.calls == ["tenant-1", "tenant-1"]
    assert refreshed is not None
    assert refreshed.total_cr == pytest.approx(99.0)
    assert refreshed.stale is False


@pytest.mark.asyncio
async def test_refresh_bypasses_a_warm_cache() -> None:
    clock = _Clock()
    client = _FakeClient(balances=[_balance(15.0), _balance(99.0)])
    service = CloudCreditService(client=client, time_source=clock)

    await service.get_balance("tenant-1")
    forced = await service.get_balance("tenant-1", refresh=True)

    assert client.calls == ["tenant-1", "tenant-1"]
    assert forced is not None
    assert forced.total_cr == pytest.approx(99.0)


@pytest.mark.asyncio
async def test_outage_serves_last_known_good_marked_stale() -> None:
    clock = _Clock()
    client = _FakeClient(
        balances=[_balance(15.0), CreditBalanceUnavailable("upstream down")],
    )
    service = CloudCreditService(
        client=client, ttl_seconds=20.0, time_source=clock,
    )

    await service.get_balance("tenant-1")
    clock.now += 21.0
    degraded = await service.get_balance("tenant-1")

    assert degraded is not None
    assert degraded.total_cr == pytest.approx(15.0)
    assert degraded.stale is True


@pytest.mark.asyncio
async def test_outage_without_any_cached_value_returns_none() -> None:
    client = _FakeClient(balances=[CreditBalanceUnavailable("upstream down")])
    service = CloudCreditService(client=client)

    assert await service.get_balance("tenant-1") is None


@pytest.mark.asyncio
async def test_cache_is_per_tenant() -> None:
    client = _FakeClient(balances=[_balance(15.0), _balance(99.0)])
    service = CloudCreditService(client=client)

    first = await service.get_balance("tenant-1")
    second = await service.get_balance("tenant-2")

    assert client.calls == ["tenant-1", "tenant-2"]
    assert first is not None and second is not None
    assert first.total_cr == pytest.approx(15.0)
    assert second.total_cr == pytest.approx(99.0)


@pytest.mark.asyncio
async def test_blank_tenant_returns_none_without_calling_upstream() -> None:
    client = _FakeClient(balances=[_balance(15.0)])
    service = CloudCreditService(client=client)

    assert await service.get_balance("   ") is None
    assert client.calls == []
