from __future__ import annotations

import httpx
import pytest
from pytest import MonkeyPatch

from kokoro_link.contracts.cloud_auth import (
    CloudAuthRejected,
    CloudDemoSessionRejected,
    CloudAuthUpstreamError,
)
from kokoro_link.infrastructure.cloud.user_service_client import (
    CloudUserServiceClient,
)


class _MockAsyncClient(httpx.AsyncClient):
    def __init__(self, handler, **kwargs) -> None:
        super().__init__(
            transport=httpx.MockTransport(handler),
            base_url=kwargs["base_url"],
            timeout=kwargs["timeout"],
        )


@pytest.mark.asyncio
async def test_cloud_user_service_client_maps_login_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = request.read().decode()
        return httpx.Response(200, json={
            "session_token": "ys_token",
            "tenant_id": "tenant_1",
            "account_id": "acct_1",
            "role": "admin",
            "status": "active",
            "tenant_tier": "demo",
            "email": "player@example.com",
            "display_name": "Player",
            "primary_language": "en-US",
            "timezone_id": "Asia/Taipei",
        })

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: _MockAsyncClient(handler, **kwargs),
    )

    client = CloudUserServiceClient(base_url="https://users.example/")
    identity = await client.login(email="player@example.com", password="secret")

    assert seen["url"] == "https://users.example/v1/auth/login"
    assert identity.account_id == "acct_1"
    assert identity.tenant_id == "tenant_1"
    assert identity.role == "admin"
    assert identity.tenant_tier == "demo"
    assert identity.session_token == "ys_token"


@pytest.mark.asyncio
async def test_cloud_user_service_client_rejects_unauthorized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "bad credentials"})

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: _MockAsyncClient(handler, **kwargs),
    )
    client = CloudUserServiceClient(base_url="https://users.example")

    with pytest.raises(CloudAuthRejected):
        await client.login(email="player@example.com", password="bad")


@pytest.mark.asyncio
async def test_cloud_user_service_client_requires_identity_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "active"})

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: _MockAsyncClient(handler, **kwargs),
    )
    client = CloudUserServiceClient(base_url="https://users.example")

    with pytest.raises(CloudAuthUpstreamError):
        await client.login(email="player@example.com", password="secret")


@pytest.mark.asyncio
async def test_cloud_user_service_client_releases_demo_session(
    monkeypatch: MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = request.read().decode()
        return httpx.Response(204)

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: _MockAsyncClient(handler, **kwargs),
    )

    client = CloudUserServiceClient(base_url="https://users.example/")
    await client.release_demo_session(tenant_id="tenant_1", account_id="acct_1")

    assert seen["url"] == "https://users.example/internal/v1/demo/sessions/release"
    assert '"tenant_id":"tenant_1"' in str(seen["body"])
    assert '"account_id":"acct_1"' in str(seen["body"])


@pytest.mark.asyncio
async def test_cloud_user_service_client_creates_demo_session(
    monkeypatch: MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = request.read().decode()
        seen["headers"] = dict(request.headers)
        return httpx.Response(200, json={
            "session_token": "ys_demo",
            "tenant_id": "demo_tenant",
            "account_id": "demo_acct",
            "role": "member",
            "status": "active",
            "tenant_tier": "demo",
            "email": "demo@example.com",
            "display_name": "Demo Player",
        })

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: _MockAsyncClient(handler, **kwargs),
    )

    client = CloudUserServiceClient(
        base_url="https://users.example/",
        internal_service_credential=(
            "core-kid|core|yuralume-user|demo-session:create|core-secret"
        ),
    )
    identity = await client.create_demo_session(
        provider="discord",
        authorization_code="oauth-code",
        redirect_uri="https://app.example/demo/oauth/discord/callback",
        code_verifier="pkce",
        source_ip="198.51.100.44",
        device_id="device-1",
    )

    assert seen["url"] == "https://users.example/internal/v1/demo/sessions"
    body = str(seen["body"])
    assert '"provider":"discord"' in body
    assert '"authorization_code":"oauth-code"' in body
    assert '"redirect_uri":"https://app.example/demo/oauth/discord/callback"' in body
    assert '"code_verifier":"pkce"' in body
    headers = seen["headers"]
    assert isinstance(headers, dict)
    assert headers["x-yuralume-service-token"] == "core-secret"
    assert headers["x-yuralume-client-ip"] == "198.51.100.44"
    assert headers["x-yuralume-demo-device"] == "device-1"
    assert identity.account_id == "demo_acct"
    assert identity.tenant_tier == "demo"
    assert identity.session_token == "ys_demo"


@pytest.mark.asyncio
async def test_cloud_user_service_client_preserves_demo_session_error(
    monkeypatch: MonkeyPatch,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={
            "error": {
                "code": "demo_busy",
                "message": "demo slots are full",
                "retryable": True,
            },
        })

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: _MockAsyncClient(handler, **kwargs),
    )

    client = CloudUserServiceClient(base_url="https://users.example/")

    with pytest.raises(CloudDemoSessionRejected) as raised:
        await client.create_demo_session(
            provider="discord",
            authorization_code="oauth-code",
        )

    assert raised.value.status_code == 503
    assert raised.value.code == "demo_busy"
    assert raised.value.message == "demo slots are full"
    assert raised.value.retryable is True


@pytest.mark.asyncio
async def test_cloud_user_service_client_release_maps_upstream_error(
    monkeypatch: MonkeyPatch,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "busy"})

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: _MockAsyncClient(handler, **kwargs),
    )

    client = CloudUserServiceClient(base_url="https://users.example/")

    with pytest.raises(CloudAuthUpstreamError):
        await client.release_demo_session(tenant_id="tenant_1", account_id="acct_1")


@pytest.mark.asyncio
async def test_cloud_user_service_client_exchanges_hosted_play_code(
    monkeypatch: MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["path"] = request.url.path
        seen["body"] = request.read().decode()
        seen["token"] = request.headers.get("X-Internal-Token")
        return httpx.Response(200, json={
            "active": True,
            "account_id": "acct_hosted",
            "tenant_id": "tenant_hosted",
            "role": "member",
            "status": "active",
            "tenant_tier": "standard",
            "email": "player@example.com",
            "display_name": "Hosted Player",
            "primary_language": "en-US",
            "timezone_id": None,
        })

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: _MockAsyncClient(handler, **kwargs),
    )

    client = CloudUserServiceClient(
        base_url="https://users.example/",
        hosted_play_internal_token="internal-secret",
    )
    identity = await client.exchange_hosted_play_code(code="yhp_entry")

    assert seen["path"] == "/internal/v1/hosted-play/exchange"
    assert '"code":"yhp_entry"' in str(seen["body"])
    assert seen["token"] == "internal-secret"
    assert identity.account_id == "acct_hosted"
    assert identity.tenant_id == "tenant_hosted"
    assert identity.tenant_tier == "standard"


@pytest.mark.asyncio
async def test_cloud_user_service_client_exchange_omits_blank_internal_token(
    monkeypatch: MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["has_token_header"] = "X-Internal-Token" in request.headers
        return httpx.Response(200, json={
            "account_id": "acct_hosted",
            "tenant_id": "tenant_hosted",
            "role": "member",
            "status": "active",
        })

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: _MockAsyncClient(handler, **kwargs),
    )

    client = CloudUserServiceClient(base_url="https://users.example/")
    await client.exchange_hosted_play_code(code="yhp_entry")

    assert seen["has_token_header"] is False


@pytest.mark.asyncio
async def test_cloud_user_service_client_exchange_rejects_invalid_code(
    monkeypatch: MonkeyPatch,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={
            "error": {"code": "invalid_code", "message": "unknown"},
        })

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: _MockAsyncClient(handler, **kwargs),
    )

    client = CloudUserServiceClient(base_url="https://users.example/")

    with pytest.raises(CloudAuthRejected):
        await client.exchange_hosted_play_code(code="yhp_gone")


@pytest.mark.asyncio
async def test_cloud_user_service_client_exchange_maps_upstream_error(
    monkeypatch: MonkeyPatch,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: _MockAsyncClient(handler, **kwargs),
    )

    client = CloudUserServiceClient(base_url="https://users.example/")

    with pytest.raises(CloudAuthUpstreamError):
        await client.exchange_hosted_play_code(code="yhp_entry")
