"""HTTP client for the control-plane core-profile endpoint.

Mirrors ``test_tier_runtime_profile_client`` transport mocking. Covers the
optional ``X-Internal-Token`` header (M10) and the unchanged path/query.
"""

from __future__ import annotations

import httpx
import pytest

from kokoro_link.contracts.cloud_routing_profile import (
    CloudRoutingProfileUnavailable,
)
from kokoro_link.infrastructure.cloud.routing_profile_client import (
    CloudRoutingProfileClient,
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
async def test_client_hits_correct_path_without_token(monkeypatch) -> None:
    seen: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["query"] = dict(request.url.params)
        seen["has_internal_token"] = "x-internal-token" in request.headers
        return httpx.Response(200, json={"tenant_id": "t1", "tier": "plus"})

    _install(monkeypatch, handler)
    client = CloudRoutingProfileClient(base_url="https://users.example/")

    await client.get_profile(
        tenant_id="t1", account_id="a1", tier="plus", user_id="u1",
    )

    assert seen["path"] == "/internal/v1/runtime-config/core-profile"
    assert seen["query"] == {
        "tenant_id": "t1",
        "account_id": "a1",
        "user_id": "u1",
        "tier": "plus",
    }
    assert seen["has_internal_token"] is False


@pytest.mark.asyncio
async def test_client_sends_internal_token_header_when_configured(monkeypatch) -> None:
    seen: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["token"] = request.headers.get("x-internal-token")
        seen["query"] = dict(request.url.params)
        return httpx.Response(200, json={"tenant_id": "t1", "tier": "plus"})

    _install(monkeypatch, handler)
    client = CloudRoutingProfileClient(
        base_url="https://users.example", internal_token="s3cr3t",
    )

    await client.get_profile(tenant_id="t1", account_id="a1", tier="plus")

    assert seen["token"] == "s3cr3t"
    # Path/query unchanged by the header.
    assert seen["query"] == {
        "tenant_id": "t1",
        "account_id": "a1",
        "tier": "plus",
    }


@pytest.mark.asyncio
async def test_client_empty_base_url_raises_unavailable() -> None:
    client = CloudRoutingProfileClient(base_url="")

    with pytest.raises(CloudRoutingProfileUnavailable):
        await client.get_profile(tenant_id="t1", account_id="a1", tier="plus")
