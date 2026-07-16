"""HTTP/SSE client for a Baileys-compatible WhatsApp sidecar."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import httpx

_LOGGER = logging.getLogger(__name__)
SidecarEventHandler = Callable[[dict[str, Any]], Awaitable[None]]


class WhatsAppSidecarClient:
    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._transport = transport

    async def connect(
        self,
        *,
        sidecar_url: str,
        session_id: str,
        api_token: str | None,
        on_event: SidecarEventHandler,
    ) -> None:
        if not sidecar_url:
            raise ValueError("sidecar_url is required")
        if not session_id:
            raise ValueError("session_id is required")

        headers = {"Accept": "text/event-stream"}
        if api_token:
            headers["Authorization"] = f"Bearer {api_token}"
        url = f"{sidecar_url.rstrip('/')}/sessions/{session_id}/events"
        async with httpx.AsyncClient(
            transport=self._transport,
            timeout=None,
            headers=headers,
        ) as client:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                async for event in _iter_sse_events(response):
                    try:
                        await on_event(event)
                    except Exception:
                        _LOGGER.exception(
                            "WhatsApp sidecar event handler failed "
                            "session_id=%s",
                            session_id,
                        )


async def _iter_sse_events(
    response: httpx.Response,
) -> AsyncIterator[dict[str, Any]]:
    data_lines: list[str] = []
    async for line in response.aiter_lines():
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
            continue
        if line:
            continue
        event = _decode_sse_data(data_lines)
        data_lines = []
        if event is not None:
            yield event
    event = _decode_sse_data(data_lines)
    if event is not None:
        yield event


def _decode_sse_data(data_lines: list[str]) -> dict[str, Any] | None:
    if not data_lines:
        return None
    raw = "\n".join(data_lines)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        _LOGGER.warning("WhatsApp sidecar emitted non-json SSE data")
        return None
    if not isinstance(data, dict):
        _LOGGER.warning("WhatsApp sidecar emitted non-object SSE data")
        return None
    return data
