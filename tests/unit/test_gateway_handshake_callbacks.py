from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import httpx
import pytest
from websockets.exceptions import ConnectionClosedError
from websockets.frames import Close

from kokoro_link.domain.value_objects.messaging_failure import (
    MessagingCredentialError,
)
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


class _ClosingDiscordWebSocket:
    """Accepts HELLO, then closes the way Discord rejects an IDENTIFY."""

    def __init__(self, close_code: int, reason: str) -> None:
        self._payloads: list[dict] = [{"op": 10, "d": {"heartbeat_interval": 60_000}}]
        self._close = Close(close_code, reason)
        self.sent: list[dict] = []

    async def recv(self) -> str:
        if self._payloads:
            return json.dumps(self._payloads.pop(0))
        raise ConnectionClosedError(self._close, None)

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))


@pytest.mark.parametrize(
    ("close_code", "expected_fragment"),
    [
        (4004, "bot token"),
        (4014, "Message Content Intent"),
        # 4014 must not read as a bad token: the fix is a Developer Portal
        # checkbox, and pointing at the token sends the operator elsewhere.
    ],
)
@pytest.mark.asyncio
async def test_discord_credential_close_codes_become_credential_errors(
    monkeypatch: pytest.MonkeyPatch,
    close_code: int,
    expected_fragment: str,
) -> None:
    websocket = _ClosingDiscordWebSocket(close_code, "rejected")
    monkeypatch.setitem(
        sys.modules,
        "websockets",
        SimpleNamespace(connect=lambda *_args, **_kwargs: _FakeDiscordConnection(websocket)),
    )

    async def on_message(_raw, _bot_user_id) -> None:  # noqa: ANN001
        raise AssertionError("no message expected")

    client = DiscordGatewayClient(gateway_url="wss://gateway.test")
    with pytest.raises(MessagingCredentialError) as caught:
        await client.connect(
            bot_token="SECRET-BOT-TOKEN", on_message_create=on_message,
        )

    assert expected_fragment in caught.value.user_message
    # The message is rendered to the operator verbatim; it must never carry
    # the credential itself.
    assert "SECRET-BOT-TOKEN" not in caught.value.user_message


@pytest.mark.asyncio
async def test_discord_transient_close_code_stays_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """1006-style closes are ops' problem, not the operator's."""
    websocket = _ClosingDiscordWebSocket(1006, "abnormal")
    monkeypatch.setitem(
        sys.modules,
        "websockets",
        SimpleNamespace(connect=lambda *_args, **_kwargs: _FakeDiscordConnection(websocket)),
    )

    async def on_message(_raw, _bot_user_id) -> None:  # noqa: ANN001
        raise AssertionError("no message expected")

    client = DiscordGatewayClient(gateway_url="wss://gateway.test")
    with pytest.raises(ConnectionClosedError):
        await client.connect(bot_token="token", on_message_create=on_message)


@pytest.mark.asyncio
async def test_discord_gateway_url_401_raises_instead_of_connecting_anyway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rejected token must not be followed by a doomed WebSocket attempt.

    Falling back to the default gateway URL here is what produced two error
    reports per attempt — the first one not even mentioning the token.
    """
    connects = 0

    def _connect(*_args, **_kwargs):  # noqa: ANN002, ANN003
        nonlocal connects
        connects += 1
        raise AssertionError("must not connect with a rejected token")

    monkeypatch.setitem(sys.modules, "websockets", SimpleNamespace(connect=_connect))

    async def on_message(_raw, _bot_user_id) -> None:  # noqa: ANN001
        raise AssertionError("no message expected")

    client = DiscordGatewayClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(401, request=request),
        ),
    )
    with pytest.raises(MessagingCredentialError, match="HTTP 401"):
        await client.connect(bot_token="bad", on_message_create=on_message)
    assert connects == 0


@pytest.mark.asyncio
async def test_discord_gateway_url_server_error_still_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 5xx says nothing about the token, so keep the tolerant fallback."""
    websocket = _FakeDiscordWebSocket()
    urls: list[str] = []

    def _connect(url, *_args, **_kwargs):  # noqa: ANN001, ANN002, ANN003
        urls.append(url)
        return _FakeDiscordConnection(websocket)

    monkeypatch.setitem(sys.modules, "websockets", SimpleNamespace(connect=_connect))

    async def on_message(_raw, _bot_user_id) -> None:  # noqa: ANN001
        raise AssertionError("no message expected")

    client = DiscordGatewayClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(503, request=request),
        ),
    )
    with pytest.raises(RuntimeError, match="requested reconnect"):
        await client.connect(bot_token="token", on_message_create=on_message)

    assert urls == ["wss://gateway.discord.gg/?v=10&encoding=json"]


@pytest.mark.asyncio
async def test_discord_missing_bot_token_is_a_credential_error() -> None:
    async def on_message(_raw, _bot_user_id) -> None:  # noqa: ANN001
        raise AssertionError("no message expected")

    client = DiscordGatewayClient(gateway_url="wss://gateway.test")
    with pytest.raises(MessagingCredentialError, match="no bot token"):
        await client.connect(bot_token="", on_message_create=on_message)


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
