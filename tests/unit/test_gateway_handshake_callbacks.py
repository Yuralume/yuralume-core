from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import httpx
import pytest

from kokoro_link.infrastructure.messaging.discord.gateway_client import (
    DiscordGatewayClient,
)
from kokoro_link.infrastructure.messaging.whatsapp.sidecar_client import (
    WhatsAppSidecarClient,
)


class _FakeDiscordWebSocket:
    def __init__(self) -> None:
        self._payloads = [
            {"op": 10, "d": {"heartbeat_interval": 60_000}},
            {"op": 0, "t": "READY", "s": 1, "d": {"user": {"id": "bot"}}},
            {"op": 7, "d": None},
        ]
        self.sent: list[dict] = []

    async def recv(self) -> str:
        return json.dumps(self._payloads.pop(0))

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))


class _FakeDiscordConnection:
    def __init__(self, websocket: _FakeDiscordWebSocket) -> None:
        self._websocket = websocket

    async def __aenter__(self) -> _FakeDiscordWebSocket:
        return self._websocket

    async def __aexit__(self, *_args) -> None:
        return None


@pytest.mark.asyncio
async def test_discord_ready_callback_runs_only_after_ready_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    websocket = _FakeDiscordWebSocket()
    monkeypatch.setitem(
        sys.modules,
        "websockets",
        SimpleNamespace(connect=lambda *_args, **_kwargs: _FakeDiscordConnection(websocket)),
    )
    ready_calls = 0

    async def on_ready() -> None:
        nonlocal ready_calls
        ready_calls += 1

    async def on_message(_raw, _bot_user_id) -> None:  # noqa: ANN001
        raise AssertionError("no message expected")

    client = DiscordGatewayClient(gateway_url="wss://gateway.test")
    with pytest.raises(RuntimeError, match="requested reconnect"):
        await client.connect(
            bot_token="token",
            on_message_create=on_message,
            on_ready=on_ready,
        )

    assert ready_calls == 1
    assert websocket.sent[0]["op"] == 2  # IDENTIFY precedes READY


@pytest.mark.asyncio
async def test_whatsapp_connected_callback_requires_successful_sse_status() -> None:
    calls = 0

    async def on_connected() -> None:
        nonlocal calls
        calls += 1

    async def on_event(_raw) -> None:  # noqa: ANN001
        raise AssertionError("no event expected")

    ok_client = WhatsAppSidecarClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                content=b": connected\n\n",
                headers={"content-type": "text/event-stream"},
                request=request,
            )
        )
    )
    await ok_client.connect(
        sidecar_url="https://sidecar.test",
        session_id="session",
        api_token=None,
        on_event=on_event,
        on_connected=on_connected,
    )
    assert calls == 1

    failed_client = WhatsAppSidecarClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(503, request=request)
        )
    )
    with pytest.raises(httpx.HTTPStatusError):
        await failed_client.connect(
            sidecar_url="https://sidecar.test",
            session_id="session",
            api_token=None,
            on_event=on_event,
            on_connected=on_connected,
        )
    assert calls == 1
