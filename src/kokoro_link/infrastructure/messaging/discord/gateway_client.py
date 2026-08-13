"""Discord Gateway WebSocket client.

The import of ``websockets`` is intentionally local to ``connect`` so unit
tests can exercise parser/service logic without requiring a live socket.

This adapter is where Discord's *protocol* signals get translated into
the domain's failure vocabulary: an HTTP 401/403 on ``/gateway/bot`` and
gateway close codes 4004 / 4014 are the platform saying "these
credentials are wrong", so they surface as
:class:`MessagingCredentialError` instead of as an opaque connection
error the service would retry forever. Everything else stays an ordinary
exception and remains retryable. The mapping reads numeric status /
close codes only — never the text of an error message.

Being a credential failure and being *unrecoverable without an edit* are
two different things, and this adapter is the only layer that knows
which is which. 4004 (*authentication failed*) is repaired by pasting a
new token, so it carries ``credentials_must_change=True`` and licenses
the worker to park the account until the stored credentials change.
4014 (*disallowed intents*) is repaired by ticking Message Content
Intent in the Developer Portal — the stored token stays byte-identical,
so parking on a credentials change would wait forever. It therefore
carries ``credentials_must_change=False`` and keeps reconnecting; since
Message Content Intent is off by default, 4014 is the *normal* first
connection of a new bot, not an edge case.
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

from kokoro_link.domain.value_objects.messaging_failure import (
    MessagingCredentialError,
)

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
_CREDENTIAL_HTTP_STATUSES = frozenset({401, 403})
_CLOSE_AUTHENTICATION_FAILED = 4004
_CLOSE_DISALLOWED_INTENTS = 4014
_CREDENTIAL_CLOSE_RESPONSES: dict[int, tuple[str, bool]] = {
    # (operator-facing message, credentials_must_change)
    _CLOSE_AUTHENTICATION_FAILED: (
        (
            "Discord rejected the bot token (gateway close 4004: authentication "
            "failed). Copy the token again from the Discord Developer Portal "
            "(Bot -> Reset Token invalidates the previous one) and update this "
            "account's settings."
        ),
        # Only a new token can clear this, and editing it is locally
        # observable, so the worker may stop until that happens.
        True,
    ),
    # Deliberately a different message *and* a different retry answer: 4014
    # means the token is fine and the fix is a checkbox in the Developer
    # Portal. Reporting it as a bad token sends the operator hunting in the
    # wrong place, and parking the account "until the credentials change"
    # would wait on an edit that correctly never happens.
    _CLOSE_DISALLOWED_INTENTS: (
        (
            "Discord refused the requested gateway intents (close 4014). Open "
            "the Discord Developer Portal -> your application -> Bot -> "
            "Privileged Gateway Intents and enable Message Content Intent. The "
            "bot token itself is fine, and this account keeps retrying in the "
            "background — it reconnects on its own once the intent is enabled, "
            "with nothing to restart."
        ),
        False,
    ),
}

GatewayEventHandler = Callable[[dict[str, Any], str | None], Awaitable[None]]
GatewayReadyHandler = Callable[[], Awaitable[None]]


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
        on_ready: GatewayReadyHandler | None = None,
    ) -> None:
        if not bot_token:
            raise MessagingCredentialError(
                "This Discord account has no bot token. Paste the bot token "
                "from the Discord Developer Portal into the account settings.",
                # Pasting the token *is* a credentials edit, so the worker
                # gets a local signal to resume from.
                credentials_must_change=True,
            )

        try:
            import websockets
        except ImportError as exc:  # pragma: no cover - environment config
            raise RuntimeError(
                "Discord Gateway requires the 'websockets' package",
            ) from exc

        url = self._gateway_url or await self._fetch_gateway_url(bot_token)
        try:
            await self._run_session(
                websockets,
                url=url,
                bot_token=bot_token,
                on_message_create=on_message_create,
                on_ready=on_ready,
            )
        except Exception as exc:
            credential_error = _credential_error_for_close(exc)
            if credential_error is None:
                raise
            raise credential_error from exc

    async def _run_session(
        self,
        websockets: Any,
        *,
        url: str,
        bot_token: str,
        on_message_create: GatewayEventHandler,
        on_ready: GatewayReadyHandler | None,
    ) -> None:
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
                        if on_ready is not None:
                            await on_ready()
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
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if status in _CREDENTIAL_HTTP_STATUSES:
                    # Never fall back to the default gateway URL here: this is
                    # Discord saying the token is wrong, and connecting anyway
                    # only earns a close 4004 a moment later — which is how one
                    # bad token used to produce two error reports per attempt,
                    # the first of them not even mentioning the token.
                    raise MessagingCredentialError(
                        f"Discord rejected the bot token (HTTP {status}). "
                        "Check this account's Discord bot token — regenerating "
                        "it in the Developer Portal invalidates the old one.",
                        # A rejected token: only a new one gets past this, and
                        # that edit is the worker's signal to try again.
                        credentials_must_change=True,
                    ) from exc
                _LOGGER.exception("Discord gateway URL fetch failed")
                return _DEFAULT_GATEWAY_URL
            except Exception:
                # Timeouts, DNS, 5xx: the token may well be fine, so keep the
                # tolerant degradation to the documented default endpoint.
                _LOGGER.exception("Discord gateway URL fetch failed")
                return _DEFAULT_GATEWAY_URL
        if isinstance(data, dict) and isinstance(data.get("url"), str):
            return f"{data['url'].rstrip('/')}?v=10&encoding=json"
        return _DEFAULT_GATEWAY_URL


def _credential_error_for_close(exc: BaseException) -> MessagingCredentialError | None:
    """Map a gateway close code onto a credential failure, or ``None``.

    Only the numeric close code decides. Anything that is not a close
    with a code we know to mean "wrong credentials" falls through to the
    caller's transient path untouched.

    The close code also carries ``credentials_must_change``: the same
    numeric signal that identifies the failure is the only thing that
    knows whether an edit to the stored credentials is what repairs it.
    """
    code = _close_code(exc)
    if code is None:
        return None
    response = _CREDENTIAL_CLOSE_RESPONSES.get(code)
    if response is None:
        return None
    message, credentials_must_change = response
    return MessagingCredentialError(
        message,
        credentials_must_change=credentials_must_change,
    )


def _close_code(exc: BaseException) -> int | None:
    """Numeric close code carried by a ``websockets`` ``ConnectionClosed``.

    ``websockets`` (pinned ``>=13.0,<16.0``; the lock resolves 15.0.1)
    exposes the received close frame as ``exc.rcvd``, whose ``.code`` is
    the protocol value — ``ConnectionClosed.code`` itself has been a
    deprecated shim since 13.1 and emits a ``DeprecationWarning``. Read
    through ``rcvd`` by attribute so a non-``websockets`` exception (or a
    close with no frame received) simply yields ``None`` instead of
    needing the exception class imported into this module, whose
    ``websockets`` import is deliberately deferred into ``connect``.
    """
    received = getattr(exc, "rcvd", None)
    code = getattr(received, "code", None)
    return code if isinstance(code, int) else None


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
