"""HTTP client for the control-plane per-tier runtime-profile endpoint.

Mirrors ``test_cloud_user_service_client`` transport mocking: 200 parses the
``profile`` payload into an ``AccountRuntimeProfile`` (named after the tier),
404 means "no control-plane profile for this tier" → ``None``, and every other
non-2xx / transport error raises ``TierRuntimeProfileUnavailable``.
"""

from __future__ import annotations

import httpx
import pytest

from kokoro_link.contracts.cloud_tier_runtime_profile import (
    TierRuntimeProfileUnavailable,
)
from kokoro_link.infrastructure.cloud.tier_runtime_profile_client import (
    TierRuntimeProfileClient,
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
async def test_client_parses_profile_and_hits_correct_path(monkeypatch) -> None:
    seen: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["query"] = dict(request.url.params)
        seen["has_auth"] = "authorization" in request.headers
        return httpx.Response(200, json={
            "tier": "plus",
            "profile": {
                "proactive_tick_multiplier": 2,
                "character_ttl_days": 30,
                "max_characters": 5,
                "album_generation_enabled": False,
                "tts_enabled": True,
            },
        })

    _install(monkeypatch, handler)
    client = TierRuntimeProfileClient(base_url="https://users.example/")

    profile = await client.fetch("plus")

    assert seen["path"] == "/internal/v1/runtime-config/runtime-profile"
    assert seen["query"] == {"tier": "plus"}
    # Mirrors routing_profile_client: no auth header on the runtime-config surface.
    assert seen["has_auth"] is False
    assert profile is not None
    assert profile.name == "plus"
    assert profile.proactive_tick_multiplier == 2
    assert profile.character_ttl.days == 30
    assert profile.max_characters == 5
    assert profile.album_generation_enabled is False
    assert profile.tts_enabled is True


@pytest.mark.asyncio
async def test_client_sends_internal_token_header_when_configured(monkeypatch) -> None:
    seen: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["token"] = request.headers.get("x-internal-token")
        seen["path"] = request.url.path
        seen["query"] = dict(request.url.params)
        return httpx.Response(200, json={"tier": "plus", "profile": {}})

    _install(monkeypatch, handler)
    client = TierRuntimeProfileClient(
        base_url="https://users.example", internal_token="s3cr3t",
    )

    await client.fetch("plus")

    assert seen["token"] == "s3cr3t"
    # Path/query are unchanged by the header.
    assert seen["path"] == "/internal/v1/runtime-config/runtime-profile"
    assert seen["query"] == {"tier": "plus"}


@pytest.mark.asyncio
async def test_client_omits_internal_token_header_when_blank(monkeypatch) -> None:
    seen: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["has_internal_token"] = "x-internal-token" in request.headers
        return httpx.Response(200, json={"tier": "plus", "profile": {}})

    _install(monkeypatch, handler)
    client = TierRuntimeProfileClient(base_url="https://users.example")

    await client.fetch("plus")

    assert seen["has_internal_token"] is False


@pytest.mark.asyncio
async def test_client_returns_none_on_404(monkeypatch) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "no profile for tier"})

    _install(monkeypatch, handler)
    client = TierRuntimeProfileClient(base_url="https://users.example")

    assert await client.fetch("mystery-tier") is None


@pytest.mark.asyncio
async def test_client_raises_unavailable_on_500(monkeypatch) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    _install(monkeypatch, handler)
    client = TierRuntimeProfileClient(base_url="https://users.example")

    with pytest.raises(TierRuntimeProfileUnavailable):
        await client.fetch("plus")


@pytest.mark.asyncio
async def test_client_raises_unavailable_on_transport_error(monkeypatch) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    _install(monkeypatch, handler)
    client = TierRuntimeProfileClient(base_url="https://users.example")

    with pytest.raises(TierRuntimeProfileUnavailable):
        await client.fetch("plus")


@pytest.mark.asyncio
async def test_client_empty_base_url_raises_unavailable(monkeypatch) -> None:
    client = TierRuntimeProfileClient(base_url="")

    with pytest.raises(TierRuntimeProfileUnavailable):
        await client.fetch("plus")


@pytest.mark.asyncio
async def test_client_missing_profile_key_yields_default_knobs(monkeypatch) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"tier": "plus"})

    _install(monkeypatch, handler)
    client = TierRuntimeProfileClient(base_url="https://users.example")

    profile = await client.fetch("plus")

    assert profile is not None
    assert profile.name == "plus"
    # Absent "profile" object → all knobs fall back to the permissive default.
    assert profile.max_characters is None
    assert profile.character_ttl is None
    assert profile.album_generation_enabled is True
