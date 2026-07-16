import json

import httpx
import pytest

from kokoro_link.contracts.messaging import OutboundMessage
from kokoro_link.domain.value_objects.platform import Platform
from kokoro_link.infrastructure.messaging.line.adapter import LineAdapter


def _ok_transport(captured: list[httpx.Request]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={})

    return httpx.MockTransport(handler)


def _outbound(**overrides: object) -> OutboundMessage:
    defaults: dict[str, object] = {
        "platform": Platform.LINE,
        "chat_ref": "U1",
        "text": "hi",
        "credentials": {"channel_access_token": "AT"},
    }
    defaults.update(overrides)
    return OutboundMessage(**defaults)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_send_uses_credentials_from_message() -> None:
    captured: list[httpx.Request] = []
    adapter = LineAdapter(transport=_ok_transport(captured))

    await adapter.send(_outbound())

    assert len(captured) == 1
    request = captured[0]
    assert request.url.path == "/v2/bot/message/push"
    assert request.headers["Authorization"] == "Bearer AT"
    body = json.loads(request.content.decode())
    assert body == {"to": "U1", "messages": [{"type": "text", "text": "hi"}]}


@pytest.mark.asyncio
async def test_send_uses_per_call_credentials() -> None:
    captured: list[httpx.Request] = []
    adapter = LineAdapter(transport=_ok_transport(captured))

    await adapter.send(_outbound(credentials={"channel_access_token": "alpha"}))
    await adapter.send(_outbound(credentials={"channel_access_token": "beta"}))

    assert [r.headers["Authorization"] for r in captured] == [
        "Bearer alpha", "Bearer beta",
    ]


@pytest.mark.asyncio
async def test_send_skips_silently_on_missing_token(
    caplog: pytest.LogCaptureFixture,
) -> None:
    adapter = LineAdapter()
    with caplog.at_level("WARNING"):
        await adapter.send(_outbound(credentials={}))
    assert any(
        "missing channel_access_token" in r.message for r in caplog.records
    )


@pytest.mark.asyncio
async def test_send_rejects_mismatched_platform() -> None:
    adapter = LineAdapter()
    with pytest.raises(ValueError):
        await adapter.send(_outbound(platform=Platform.TELEGRAM))


@pytest.mark.asyncio
async def test_send_swallows_4xx(caplog: pytest.LogCaptureFixture) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "Authentication failed"})

    adapter = LineAdapter(transport=httpx.MockTransport(handler))
    with caplog.at_level("WARNING"):
        await adapter.send(_outbound())
    assert any("LINE push failed" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_send_swallows_transport_error(caplog: pytest.LogCaptureFixture) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    adapter = LineAdapter(transport=httpx.MockTransport(handler))
    with caplog.at_level("ERROR"):
        await adapter.send(_outbound())
    assert any(
        "LINE push transport error" in r.message for r in caplog.records
    )


@pytest.mark.asyncio
async def test_set_webhook_endpoint_puts_url() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={})

    adapter = LineAdapter(transport=httpx.MockTransport(handler))
    result = await adapter.set_webhook_endpoint(
        channel_access_token="AT",
        webhook_url="https://example.test/hook",
    )

    assert result == {"ok": True}
    request = captured[0]
    assert request.method == "PUT"
    assert request.url.path == "/v2/bot/channel/webhook/endpoint"
    assert request.headers["Authorization"] == "Bearer AT"
    assert json.loads(request.content.decode()) == {
        "endpoint": "https://example.test/hook",
    }


@pytest.mark.asyncio
async def test_set_webhook_endpoint_reports_4xx() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "invalid token"})

    adapter = LineAdapter(transport=httpx.MockTransport(handler))
    result = await adapter.set_webhook_endpoint(
        channel_access_token="AT", webhook_url="https://example.test/h",
    )
    assert result["ok"] is False
    assert "401" in result["error"]


@pytest.mark.asyncio
async def test_get_webhook_endpoint_returns_payload() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        return httpx.Response(
            200,
            json={
                "endpoint": "https://example.test/hook",
                "active": True,
            },
        )

    adapter = LineAdapter(transport=httpx.MockTransport(handler))
    result = await adapter.get_webhook_endpoint(channel_access_token="AT")

    assert result["ok"] is True
    assert result["endpoint"] == "https://example.test/hook"
    assert result["active"] is True
