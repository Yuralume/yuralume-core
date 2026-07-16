from __future__ import annotations

import json
import logging

import httpx
import pytest

from kokoro_link.contracts.cloud_gateway import CloudGatewayIdentity
from kokoro_link.infrastructure.llm.cloud_gateway_model import (
    CloudGatewayChatModel,
)
from kokoro_link.infrastructure.llm.cloud_refusal import ExpectedCloudRefusal


class _MockAsyncClient(httpx.AsyncClient):
    def __init__(self, handler, **kwargs) -> None:
        super().__init__(
            transport=httpx.MockTransport(handler),
            timeout=kwargs["timeout"],
        )


@pytest.mark.asyncio
async def test_cloud_gateway_model_posts_openai_shape_with_cloud_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["payload"] = json.loads(request.content.decode())
        return httpx.Response(200, json={
            "choices": [
                {"message": {"content": "hello from gateway"}},
            ],
        })

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: _MockAsyncClient(handler, **kwargs),
    )
    model = CloudGatewayChatModel(
        base_url="https://gateway.example/",
        deployment_token="ykl_deploy",
        default_model="preset-chat",
        feature_key="chat",
        identity=CloudGatewayIdentity(
            operator_id="cloud:acct_1",
            account_id="acct_1",
            tenant_id="tenant_1",
            character_ref="chr_abc",
        ),
    )

    result = await model.generate("Say hi", model="preset-fast")

    assert result == "hello from gateway"
    assert seen["url"] == "https://gateway.example/v1/chat/completions"
    headers = seen["headers"]
    assert headers["authorization"] == "Bearer ykl_deploy"
    assert headers["x-yuralume-deployment"] == "hosted-primary"
    assert headers["x-yuralume-audience"] == "yuralume-gateway"
    assert headers["x-yuralume-account"] == "acct_1"
    assert headers["x-yuralume-tenant"] == "tenant_1"
    assert headers["x-yuralume-feature"] == "chat"
    assert headers["x-yuralume-character"] == "chr_abc"
    assert str(headers["x-request-id"]).startswith("llm-")
    assert model.last_request_id == headers["x-request-id"]
    payload = seen["payload"]
    assert payload["model"] == "preset-fast"
    assert payload["messages"][1] == {"role": "user", "content": "Say hi"}


@pytest.mark.asyncio
async def test_cloud_gateway_model_supports_vision_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["payload"] = json.loads(request.content.decode())
        return httpx.Response(200, json={
            "choices": [
                {"message": {"content": "vision ok"}},
            ],
        })

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: _MockAsyncClient(handler, **kwargs),
    )
    model = CloudGatewayChatModel(
        base_url="https://gateway.example",
        deployment_token="ykl_deploy",
        default_model="preset-chat",
        feature_key="chat",
        identity=CloudGatewayIdentity(
            operator_id="cloud:acct_1",
            account_id="acct_1",
            tenant_id="tenant_1",
            character_ref="chr_abc",
        ),
    )

    await model.generate("Look", image_urls=("https://img.example/a.png",))

    content = seen["payload"]["messages"][1]["content"]
    assert content[0] == {"type": "text", "text": "Look"}
    assert content[1] == {
        "type": "image_url",
        "image_url": {"url": "https://img.example/a.png"},
    }


def test_cloud_gateway_model_lists_default_model() -> None:
    model = CloudGatewayChatModel(
        base_url="https://gateway.example",
        deployment_token="ykl_deploy",
        default_model="preset-chat",
        feature_key="chat",
        identity=CloudGatewayIdentity(
            operator_id="cloud:acct_1",
            account_id="acct_1",
            tenant_id="tenant_1",
            character_ref="chr_abc",
        ),
    )

    assert model.provider_id == "yuralume_cloud"
    assert model.supports_vision is True


@pytest.mark.asyncio
async def test_cloud_gateway_model_http_error_logs_upstream_body(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={
            "error": {"message": "invalid model preset"},
        })

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: _MockAsyncClient(handler, **kwargs),
    )
    caplog.set_level(
        logging.ERROR,
        logger="kokoro_link.infrastructure.llm.cloud_gateway_model",
    )
    model = CloudGatewayChatModel(
        base_url="https://gateway.example",
        deployment_token="ykl_deploy",
        default_model="preset-chat",
        feature_key="chat",
        identity=CloudGatewayIdentity(
            operator_id="cloud:acct_1",
            account_id="acct_1",
            tenant_id="tenant_1",
            character_ref="chr_abc",
        ),
    )

    with pytest.raises(httpx.HTTPStatusError):
        await model.generate("Say hi", model="bad-preset")

    assert "Cloud Gateway LLM" in caplog.text
    assert "HTTP 400" in caplog.text
    assert "invalid model preset" in caplog.text
    assert "https://gateway.example/v1/chat/completions" in caplog.text


@pytest.mark.asyncio
async def test_cloud_gateway_model_entitlement_denied_warns_not_errors(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={
            "error": {
                "code": "entitlement_denied",
                "message": "forwarded account is inactive or outside the forwarded tenant",
                "retryable": False,
            },
        })

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: _MockAsyncClient(handler, **kwargs),
    )
    caplog.set_level(
        logging.DEBUG,
        logger="kokoro_link.infrastructure.llm.cloud_gateway_model",
    )
    model = CloudGatewayChatModel(
        base_url="https://gateway.example",
        deployment_token="ykl_deploy",
        default_model="preset-chat",
        feature_key="dialogue_summary",
        identity=CloudGatewayIdentity(
            operator_id="cloud:acct_1",
            account_id="acct_1",
            tenant_id="tenant_1",
            character_ref="chr_abc",
        ),
    )

    with pytest.raises(ExpectedCloudRefusal) as excinfo:
        await model.generate("Say hi")

    # Subclasses HTTPStatusError so existing transport handling still catches it.
    assert isinstance(excinfo.value, httpx.HTTPStatusError)
    assert excinfo.value.code == "entitlement_denied"

    # A deliberate refusal is a WARNING with identity context — never an ERROR.
    assert not any(r.levelno >= logging.ERROR for r in caplog.records)
    refusals = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(refusals) == 1
    text = refusals[0].getMessage()
    assert "entitlement_denied" in text
    assert "feature=dialogue_summary" in text
    assert "character=chr_abc" in text
    assert "account=acct_1" in text
