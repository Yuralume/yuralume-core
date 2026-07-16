import json

import httpx
import pytest

from kokoro_link.contracts.messaging import OutboundMessage
from kokoro_link.domain.value_objects.platform import Platform
from kokoro_link.infrastructure.messaging.telegram.adapter import TelegramAdapter


def _ok_transport(captured: list[httpx.Request]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"ok": True, "result": {}})

    return httpx.MockTransport(handler)


def _outbound(**overrides: object) -> OutboundMessage:
    defaults: dict[str, object] = {
        "platform": Platform.TELEGRAM,
        "chat_ref": "99",
        "text": "hi",
        "credentials": {"bot_token": "TOKEN"},
    }
    defaults.update(overrides)
    return OutboundMessage(**defaults)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_send_uses_credentials_from_message() -> None:
    captured: list[httpx.Request] = []
    adapter = TelegramAdapter(transport=_ok_transport(captured))

    await adapter.send(_outbound())

    assert len(captured) == 1
    request = captured[0]
    assert request.url.path == "/botTOKEN/sendMessage"
    body = request.content.decode()
    assert "\"chat_id\":\"99\"" in body or '"chat_id": "99"' in body
    assert "hi" in body


@pytest.mark.asyncio
async def test_send_uses_per_call_credentials() -> None:
    """Two accounts with different tokens must hit distinct URLs."""

    captured: list[httpx.Request] = []
    adapter = TelegramAdapter(transport=_ok_transport(captured))

    await adapter.send(_outbound(credentials={"bot_token": "ALPHA"}))
    await adapter.send(_outbound(credentials={"bot_token": "BETA"}))

    paths = [r.url.path for r in captured]
    assert paths == ["/botALPHA/sendMessage", "/botBETA/sendMessage"]


@pytest.mark.asyncio
async def test_send_skips_silently_on_missing_token(
    caplog: pytest.LogCaptureFixture,
) -> None:
    adapter = TelegramAdapter()
    with caplog.at_level("WARNING"):
        await adapter.send(_outbound(credentials={}))
    assert any("missing bot_token" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_send_rejects_mismatched_platform() -> None:
    adapter = TelegramAdapter()
    with pytest.raises(ValueError):
        await adapter.send(_outbound(platform=Platform.LINE))


@pytest.mark.asyncio
async def test_send_swallows_4xx(caplog: pytest.LogCaptureFixture) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"ok": False, "description": "blocked"})

    adapter = TelegramAdapter(transport=httpx.MockTransport(handler))
    with caplog.at_level("WARNING"):
        await adapter.send(_outbound())
    assert any("Telegram sendMessage failed" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_send_swallows_transport_error(caplog: pytest.LogCaptureFixture) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    adapter = TelegramAdapter(transport=httpx.MockTransport(handler))
    with caplog.at_level("ERROR"):
        await adapter.send(_outbound())
    assert any(
        "Telegram sendMessage transport error" in r.message
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_set_webhook_posts_url_and_secret() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200, json={"ok": True, "result": True, "description": "ok"},
        )

    adapter = TelegramAdapter(transport=httpx.MockTransport(handler))
    result = await adapter.set_webhook(
        bot_token="TOKEN",
        webhook_url="https://example.test/api/v1/messaging/telegram/webhook/xyz",
        secret_token="s3cret",
    )

    assert result == {"ok": True, "result": True, "description": "ok"}
    assert len(captured) == 1
    request = captured[0]
    assert request.url.path == "/botTOKEN/setWebhook"
    body = json.loads(request.content.decode())
    assert body == {
        "url": "https://example.test/api/v1/messaging/telegram/webhook/xyz",
        "secret_token": "s3cret",
    }


@pytest.mark.asyncio
async def test_set_webhook_without_secret_omits_field() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"ok": True, "result": True})

    adapter = TelegramAdapter(transport=httpx.MockTransport(handler))
    await adapter.set_webhook(
        bot_token="TOKEN", webhook_url="https://example.test/hook",
    )

    body = json.loads(captured[0].content.decode())
    assert body == {"url": "https://example.test/hook"}


@pytest.mark.asyncio
async def test_set_webhook_reports_transport_error_as_ok_false() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("unreachable", request=request)

    adapter = TelegramAdapter(transport=httpx.MockTransport(handler))
    result = await adapter.set_webhook(
        bot_token="T", webhook_url="https://example.test/hook",
    )
    assert result == {"ok": False, "error": "unreachable"}


@pytest.mark.asyncio
async def test_get_webhook_info_returns_raw_payload() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/botTOKEN/getWebhookInfo"
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "url": "https://example.test/hook",
                    "pending_update_count": 0,
                    "last_error_message": "",
                },
            },
        )

    adapter = TelegramAdapter(transport=httpx.MockTransport(handler))
    result = await adapter.get_webhook_info(bot_token="TOKEN")

    assert result["ok"] is True
    assert result["result"]["url"] == "https://example.test/hook"


@pytest.mark.asyncio
async def test_delete_webhook_posts_drop_flag() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"ok": True, "result": True})

    adapter = TelegramAdapter(transport=httpx.MockTransport(handler))
    result = await adapter.delete_webhook(
        bot_token="TOKEN", drop_pending_updates=False,
    )

    assert result["ok"] is True
    assert captured[0].url.path == "/botTOKEN/deleteWebhook"
    assert json.loads(captured[0].content.decode()) == {
        "drop_pending_updates": False,
    }


@pytest.mark.asyncio
async def test_get_updates_posts_offset_timeout_and_allowed_updates() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={"ok": True, "result": [{"update_id": 42}]},
        )

    adapter = TelegramAdapter(transport=httpx.MockTransport(handler))
    result = await adapter.get_updates(
        bot_token="TOKEN",
        offset=40,
        timeout_seconds=25,
        limit=50,
    )

    assert result["result"] == [{"update_id": 42}]
    assert captured[0].url.path == "/botTOKEN/getUpdates"
    assert json.loads(captured[0].content.decode()) == {
        "timeout": 25,
        "limit": 50,
        "allowed_updates": ["message"],
        "offset": 40,
    }
