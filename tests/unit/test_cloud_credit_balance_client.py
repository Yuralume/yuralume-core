"""U3 — Core-side HTTP client for the User service internal balance endpoint.

Mirrors ``test_cloud_routing_profile_client`` transport mocking. Covers the
versioned service-credential headers, the path/query contract, and the typed
``CreditBalanceUnavailable`` mapping the cache relies on for fail-soft.
"""

from __future__ import annotations

import httpx
import pytest

from kokoro_link.contracts.cloud_credit_balance import (
    CreditBalanceUnavailable,
)
from kokoro_link.infrastructure.cloud.credit_balance_client import (
    CreditBalanceClient,
)

_CREDENTIAL = (
    "core-kid|core|yuralume-user|credits:read|core-secret"
)


class _MockAsyncClient(httpx.AsyncClient):
    def __init__(self, handler, **kwargs) -> None:
        super().__init__(
            transport=httpx.MockTransport(handler),
            base_url=kwargs["base_url"],
            timeout=kwargs["timeout"],
        )


def _install(monkeypatch, handler) -> None:
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: _MockAsyncClient(handler, **kwargs),
    )


@pytest.mark.asyncio
async def test_client_reads_balance_with_service_credential(monkeypatch) -> None:
    seen: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["query"] = dict(request.url.params)
        seen["caller"] = request.headers.get("x-yuralume-service-caller")
        seen["audience"] = request.headers.get("x-yuralume-service-audience")
        seen["scope"] = request.headers.get("x-yuralume-service-scope")
        return httpx.Response(
            200,
            json={
                "gift_cr": 5.0,
                "purchased_cr": 10.0,
                "total_cr": 15.0,
                "next_gift_expiry": "2026-08-01T00:00:00Z",
            },
        )

    _install(monkeypatch, handler)
    client = CreditBalanceClient(
        base_url="https://users.example/",
        internal_credential=_CREDENTIAL,
    )

    balance = await client.fetch("tenant-1")

    assert seen["path"] == "/internal/v1/credits/balance"
    assert seen["query"] == {"tenant_id": "tenant-1"}
    assert seen["caller"] == "core"
    assert seen["audience"] == "yuralume-user"
    assert seen["scope"] == "credits:read"
    assert balance.gift_cr == pytest.approx(5.0)
    assert balance.purchased_cr == pytest.approx(10.0)
    assert balance.total_cr == pytest.approx(15.0)
    assert balance.next_gift_expiry == "2026-08-01T00:00:00Z"


@pytest.mark.asyncio
async def test_client_has_no_legacy_internal_token_path(monkeypatch) -> None:
    """Cloud marks this endpoint legacy-ineligible: a legacy token answers 401.

    So the client neither accepts a legacy knob nor emits the header when the
    credential is missing — an unconfigured deployment must fail loudly at the
    credential, not quietly authenticate as nobody.
    """
    seen: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["legacy"] = "x-internal-token" in request.headers
        return httpx.Response(
            200,
            json={"gift_cr": 0, "purchased_cr": 0, "total_cr": 0},
        )

    _install(monkeypatch, handler)
    with pytest.raises(TypeError):
        CreditBalanceClient(  # type: ignore[call-arg]
            base_url="https://users.example",
            internal_token="legacy-secret",
        )

    client = CreditBalanceClient(base_url="https://users.example")
    await client.fetch("tenant-1")

    assert seen["legacy"] is False


@pytest.mark.asyncio
async def test_unknown_tenant_reads_as_all_zero(monkeypatch) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "gift_cr": 0,
                "purchased_cr": 0,
                "total_cr": 0,
                "next_gift_expiry": None,
            },
        )

    _install(monkeypatch, handler)
    client = CreditBalanceClient(base_url="https://users.example")

    balance = await client.fetch("ghost-tenant")

    assert balance.total_cr == pytest.approx(0.0)
    assert balance.next_gift_expiry is None


@pytest.mark.asyncio
async def test_empty_base_url_raises_unavailable() -> None:
    client = CreditBalanceClient(base_url="")

    with pytest.raises(CreditBalanceUnavailable):
        await client.fetch("tenant-1")


@pytest.mark.asyncio
async def test_blank_tenant_raises_unavailable() -> None:
    client = CreditBalanceClient(base_url="https://users.example")

    with pytest.raises(CreditBalanceUnavailable):
        await client.fetch("   ")


@pytest.mark.asyncio
async def test_server_error_raises_unavailable(monkeypatch) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    _install(monkeypatch, handler)
    client = CreditBalanceClient(base_url="https://users.example")

    with pytest.raises(CreditBalanceUnavailable):
        await client.fetch("tenant-1")


@pytest.mark.asyncio
async def test_transport_error_raises_unavailable(monkeypatch) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out")

    _install(monkeypatch, handler)
    client = CreditBalanceClient(base_url="https://users.example")

    with pytest.raises(CreditBalanceUnavailable):
        await client.fetch("tenant-1")


@pytest.mark.asyncio
async def test_non_json_body_raises_unavailable(monkeypatch) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")

    _install(monkeypatch, handler)
    client = CreditBalanceClient(base_url="https://users.example")

    with pytest.raises(CreditBalanceUnavailable):
        await client.fetch("tenant-1")


@pytest.mark.asyncio
async def test_quoted_decimals_are_accepted(monkeypatch) -> None:
    """Jackson may serialise the ledger's ``BigDecimal`` as a string."""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "gift_cr": "2.5",
                "purchased_cr": "10.0",
                "total_cr": "12.5",
            },
        )

    _install(monkeypatch, handler)
    client = CreditBalanceClient(base_url="https://users.example")

    balance = await client.fetch("tenant-1")

    assert balance.total_cr == pytest.approx(12.5)
    assert balance.gift_cr == pytest.approx(2.5)
    assert balance.purchased_cr == pytest.approx(10.0)


@pytest.mark.asyncio
async def test_a_missing_amount_field_raises_unavailable(monkeypatch) -> None:
    """A partial body used to read as "0 螢火" and was cached as a fresh
    snapshot — the player saw an empty wallet they had actually paid for."""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"total_cr": "12.5"})

    _install(monkeypatch, handler)
    client = CreditBalanceClient(base_url="https://users.example")

    with pytest.raises(CreditBalanceUnavailable):
        await client.fetch("tenant-1")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "broken",
    [
        {"gift_cr": "abc", "purchased_cr": 1, "total_cr": 1},
        {"gift_cr": None, "purchased_cr": 1, "total_cr": 1},
        {"gift_cr": True, "purchased_cr": 1, "total_cr": 2},
        {"gift_cr": "NaN", "purchased_cr": 1, "total_cr": 1},
        {"gift_cr": "Infinity", "purchased_cr": 1, "total_cr": 1},
        {"gift_cr": {"amount": 1}, "purchased_cr": 1, "total_cr": 2},
    ],
)
async def test_an_unusable_amount_raises_unavailable(
    monkeypatch, broken: dict,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=broken)

    _install(monkeypatch, handler)
    client = CreditBalanceClient(base_url="https://users.example")

    with pytest.raises(CreditBalanceUnavailable):
        await client.fetch("tenant-1")


@pytest.mark.asyncio
async def test_a_total_that_contradicts_the_pools_raises_unavailable(
    monkeypatch,
) -> None:
    """The two pools and the total come from one ledger row; a mismatch means
    we are reading something other than a balance."""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"gift_cr": 5.0, "purchased_cr": 10.0, "total_cr": 99.0},
        )

    _install(monkeypatch, handler)
    client = CreditBalanceClient(base_url="https://users.example")

    with pytest.raises(CreditBalanceUnavailable):
        await client.fetch("tenant-1")


@pytest.mark.asyncio
async def test_decimal_rounding_slack_is_tolerated(monkeypatch) -> None:
    """Upstream rounds each field independently; sub-cent drift is normal."""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"gift_cr": 5.004, "purchased_cr": 10.0, "total_cr": 15.0},
        )

    _install(monkeypatch, handler)
    client = CreditBalanceClient(base_url="https://users.example")

    balance = await client.fetch("tenant-1")

    assert balance.total_cr == pytest.approx(15.0)
