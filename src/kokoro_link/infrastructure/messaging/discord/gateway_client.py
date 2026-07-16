"""Discord Gateway WebSocket client.

The import of ``websockets`` is intentionally local to ``connect`` so unit
tests can exercise parser/service logic without requiring a live socket.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

_LOGGER = logging.getLogger(__name__)
_DEFAULT_API_BASE = "https://discord.com/api/v10"
_DEFAULT_GATEWAY_URL = "wss://gateway.discord.gg/?v=10&encoding=json"
_REQUEST_TIMEOUT_SECONDS = 15.0
_OP_DISPATCH = 0
_OP_HEARTBEAT = 1
_OP_IDENTIFY = 2
_OP_RECONNECT = 7
_OP_INVALID_SESSION = 9
_OP_HELLO = 10
_OP_HEARTBEAT_ACK = 11
_INTENT_GUILDS = 1 << 0
_INTENT_GUILD_MESSAGES = 1 << 9
_INTENT_DIRECT_MESSAGES = 1 << 12
_INTENT_MESSAGE_CONTENT = 1 << 15
_DEFAULT_INTENTS = (
    _INTENT_GUILDS
    | _INTENT_GUILD_MESSAGES
    | _INTENT_DIRECT_MESSAGES
    | _INTENT_MESSAGE_CONTENT
)

GatewayEventHandler = Callable[[dict[str, Any], str | None], Awaitable[None]]


class DiscordGatewayClient:
    def __init__(
        self,
        *,
        api_base: str = _DEFAULT_API_BASE,
        gateway_url: str | None = None,
        intents: int = _DEFAULT_INTENTS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_base = api_base.rstrip("/")
        self._gateway_url = gateway_url
        self._intents = intents
        self._transport = transport

    async def connect(
        self,
        *,
        bot_token: str,
        on_message_create: GatewayEventHandler,
    ) -> None:
        if not bot_token:
            raise ValueError("bot_token is required")

        try:
            import websockets
        except ImportError as exc:  # pragma: no cover - environment config
            raise RuntimeError(
                "Discord Gateway requires the 'websockets' package",
            ) from exc

        url = self._gateway_url or await self._fetch_gateway_url(bot_token)
        async with websockets.connect(url, ping_interval=None) as websocket:
            hello = await _recv_payload(websocket)
            if hello.get("op") != _OP_HELLO:
                raise RuntimeError("Discord Gateway did not send HELLO first")
            interval_ms = _heartbeat_interval(hello)
            sequence: int | None = None
            heartbeat_task = asyncio.create_task(
                _heartbeat_loop(websocket, lambda: sequence, interval_ms),
                name="discord-gateway-heartbeat",
            )
            bot_user_id: str | None = None
            try:
                await _send_json(websocket, _identify_payload(bot_token, self._intents))
                while True:
                    payload = await _recv_payload(websocket)
                    op = payload.get("op")
                    if isinstance(payload.get("s"), int):
                        sequence = payload["s"]
                    if op == _OP_HEARTBEAT:
                        await _send_json(websocket, {"op": _OP_HEARTBEAT, "d": sequence})
                        continue
                    if op == _OP_HEARTBEAT_ACK:
                        continue
                    if op == _OP_RECONNECT:
                        raise RuntimeError("Discord Gateway requested reconnect")
                    if op == _OP_INVALID_SESSION:
                        raise RuntimeError("Discord Gateway invalidated session")
                    if op != _OP_DISPATCH:
                        continue
                    event_name = payload.get("t")
                    data = payload.get("d")
                    if not isinstance(data, dict):
                        continue
                    if event_name == "READY":
                        bot_user_id = _ready_user_id(data)
                        continue
                    if event_name == "MESSAGE_CREATE":
                        await on_message_create(data, bot_user_id)
            finally:
                heartbeat_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat_task

    async def _fetch_gateway_url(self, bot_token: str) -> str:
        url = f"{self._api_base}/gateway/bot"
        headers = {"Authorization": f"Bot {bot_token}"}
        async with httpx.AsyncClient(
            transport=self._transport,
            timeout=_REQUEST_TIMEOUT_SECONDS,
            headers=headers,
        ) as client:
            try:
                response = await client.get(url)
                response.raise_for_status()
                data = response.json()
            except Exception:
                _LOGGER.exception("Discord gateway URL fetch failed")
                return _DEFAULT_GATEWAY_URL
        if isinstance(data, dict) and isinstance(data.get("url"), str):
            return f"{data['url'].rstrip('/')}?v=10&encoding=json"
        return _DEFAULT_GATEWAY_URL


async def _heartbeat_loop(
    websocket: Any,
    sequence_getter: Callable[[], int | None],
    interval_ms: int,
) -> None:
    await asyncio.sleep((interval_ms / 1000.0) * random.random())
    while True:
        await _send_json(
            websocket,
            {"op": _OP_HEARTBEAT, "d": sequence_getter()},
        )
        await asyncio.sleep(interval_ms / 1000.0)


async def _recv_payload(websocket: Any) -> dict[str, Any]:
    raw = await websocket.recv()
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise RuntimeError("Discord Gateway returned a non-object payload")
    return data


async def _send_json(websocket: Any, payload: dict[str, Any]) -> None:
    await websocket.send(json.dumps(payload, separators=(",", ":")))


def _heartbeat_interval(hello: dict[str, Any]) -> int:
    data = hello.get("d")
    if isinstance(data, dict) and isinstance(data.get("heartbeat_interval"), int):
        return data["heartbeat_interval"]
    raise RuntimeError("Discord Gateway HELLO missing heartbeat interval")


def _identify_payload(bot_token: str, intents: int) -> dict[str, Any]:
    return {
        "op": _OP_IDENTIFY,
        "d": {
            "token": bot_token,
            "intents": intents,
            "properties": {
                "os": "windows",
                "browser": "yuralume",
                "device": "yuralume",
            },
        },
    }


def _ready_user_id(data: dict[str, Any]) -> str | None:
    user = data.get("user")
    if not isinstance(user, dict):
        return None
    raw = user.get("id")
    return raw if isinstance(raw, str) and raw else None
