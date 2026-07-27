from __future__ import annotations

import json

import httpx
import pytest

from kokoro_link.contracts.cloud_gateway import CloudGatewayIdentity
from kokoro_link.contracts.cloud_routing_profile import CloudRoutingProfile
from kokoro_link.contracts.embedder import EmbedderError
from kokoro_link.contracts.generation_trigger import (
    GenerationTrigger,
    generation_trigger_scope,
)
from kokoro_link.infrastructure.embedder.cloud_gateway import CloudGatewayEmbedder


class _MockAsyncClient(httpx.AsyncClient):
    def __init__(self, handler, **kwargs) -> None:
        super().__init__(
            transport=httpx.MockTransport(handler),
            timeout=kwargs["timeout"],
        )


class _ProfilePort:
    def __init__(self, profile: CloudRoutingProfile) -> None:
        self.profile = profile
        self.calls: list[dict[str, str]] = []

    async def get_profile(
        self, *, tenant_id: str, account_id: str, tier: str, user_id: str = ""
    ) -> CloudRoutingProfile:
        self.calls.append({
            "tenant_id": tenant_id,
            "account_id": account_id,
            "tier": tier,
            "user_id": user_id,
        })
        return self.profile


def _identity() -> CloudGatewayIdentity:
    return CloudGatewayIdentity(
        operator_id="cloud:acct_1",
        account_id="acct_1",
        tenant_id="tenant_1",
        character_ref="",
        tenant_tier="standard",
    )


@pytest.mark.asyncio
async def test_cloud_embedder_posts_openai_shape_and_identity_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["payload"] = json.loads(request.content.decode())
        return httpx.Response(200, json={
            "data": [
                {"index": 1, "embedding": [2.0]},
                {"index": 0, "embedding": [1.0]},
            ],
        })

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: _MockAsyncClient(handler, **kwargs),
    )
    embedder = CloudGatewayEmbedder(
        base_url="https://gateway.example/",
        deployment_token="ykl_deploy",
        default_model="embedding-default",
        dimension=1,
        identity=_identity(),
    )

    with generation_trigger_scope(GenerationTrigger.BACKGROUND):
        vectors = await embedder.embed_many(["first", "second"])

    assert vectors == [(1.0,), (2.0,)]
    assert seen["url"] == "https://gateway.example/v1/embeddings"
    headers = seen["headers"]
    assert headers["authorization"] == "Bearer ykl_deploy"
    assert headers["x-yuralume-deployment"] == "hosted-primary"
    assert headers["x-yuralume-audience"] == "yuralume-gateway"
    assert headers["x-yuralume-tenant"] == "tenant_1"
    assert headers["x-yuralume-account"] == "acct_1"
    assert headers["x-yuralume-feature"] == "embedding"
    assert headers["x-yuralume-trigger"] == "v1;source=background"
    assert str(headers["x-request-id"]).startswith("embedding-")
    assert seen["payload"] == {
        "model": "embedding-default",
        "input": ["first", "second"],
    }


@pytest.mark.asyncio
async def test_cloud_embedder_uses_optional_profile_preset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode())
        assert payload["model"] == "profile-embedding"
        return httpx.Response(200, json={
            "data": [{"index": 0, "embedding": [0.25]}],
        })

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: _MockAsyncClient(handler, **kwargs),
    )
    profile = CloudRoutingProfile.from_payload({
        "embedding_feature_presets": {"embedding": "profile-embedding"},
        "strict_no_fallback": True,
    })
    profile_port = _ProfilePort(profile)
    embedder = CloudGatewayEmbedder(
        base_url="https://gateway.example",
        deployment_token="ykl_deploy",
        default_model="env-embedding",
        dimension=1,
        identity=_identity(),
        routing_profile_port=profile_port,
    )

    assert await embedder.embed("hello") == (0.25,)
    assert profile_port.calls == [{
        "tenant_id": "tenant_1",
        "account_id": "acct_1",
        "tier": "standard",
        "user_id": "acct_1",
    }]


@pytest.mark.asyncio
async def test_missing_optional_profile_field_falls_back_to_env_preset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode())
        assert payload["model"] == "env-embedding"
        return httpx.Response(200, json={
            "data": [{"index": 0, "embedding": [0.5]}],
        })

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: _MockAsyncClient(handler, **kwargs),
    )
    profile_port = _ProfilePort(CloudRoutingProfile.from_payload({
        "strict_no_fallback": True,
    }))
    embedder = CloudGatewayEmbedder(
        base_url="https://gateway.example",
        deployment_token="ykl_deploy",
        default_model="env-embedding",
        dimension=1,
        identity=_identity(),
        routing_profile_port=profile_port,
    )

    assert await embedder.embed("hello") == (0.5,)


@pytest.mark.asyncio
async def test_cloud_embedder_http_failure_is_embedder_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "unavailable"})

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: _MockAsyncClient(handler, **kwargs),
    )
    embedder = CloudGatewayEmbedder(
        base_url="https://gateway.example",
        deployment_token="ykl_deploy",
        default_model="embedding-default",
        dimension=1,
        identity=_identity(),
    )

    with pytest.raises(EmbedderError):
        await embedder.embed("hello")
