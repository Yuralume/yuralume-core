"""WhatsApp outbound adapter using a Baileys-compatible sidecar."""

from __future__ import annotations

import logging

import httpx

from kokoro_link.contracts.messaging import ChannelAdapterPort, OutboundMessage
from kokoro_link.domain.value_objects.platform import Platform

_LOGGER = logging.getLogger(__name__)
_REQUEST_TIMEOUT_SECONDS = 20.0


class WhatsAppAdapter(ChannelAdapterPort):
    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._transport = transport

    @property
    def platform(self) -> Platform:
        return Platform.WHATSAPP

    async def send(self, message: OutboundMessage) -> None:
        if message.platform != Platform.WHATSAPP:
            raise ValueError(
                f"WhatsAppAdapter cannot handle platform {message.platform.value}",
            )
        sidecar_url = message.credentials.get("sidecar_url", "").strip().rstrip("/")
        session_id = message.credentials.get("session_id", "").strip()
        api_token = message.credentials.get("api_token", "").strip()
        if not sidecar_url or not session_id:
            _LOGGER.warning(
                "WhatsApp send skipped — missing sidecar_url/session_id "
                "for chat_ref=%s",
                message.chat_ref,
            )
            return
        if not message.text.strip() and not message.attachments:
            _LOGGER.debug(
                "WhatsApp send skipped — empty message for chat_ref=%s",
                message.chat_ref,
            )
            return

        headers = {"Content-Type": "application/json"}
        if api_token:
            headers["Authorization"] = f"Bearer {api_token}"
        payload = {
            "chat_ref": message.chat_ref,
            "text": message.text,
            "attachments": [
                {
                    "kind": attachment.kind,
                    "url": attachment.url,
                    "mime_type": attachment.mime_type,
                    "caption": attachment.caption,
                }
                for attachment in message.attachments
            ],
        }

        async with httpx.AsyncClient(
            transport=self._transport,
            timeout=_REQUEST_TIMEOUT_SECONDS,
            headers=headers,
        ) as client:
            await self._post_message(
                client,
                sidecar_url=sidecar_url,
                session_id=session_id,
                payload=payload,
            )

    async def _post_message(
        self,
        client: httpx.AsyncClient,
        *,
        sidecar_url: str,
        session_id: str,
        payload: dict[str, object],
    ) -> None:
        url = f"{sidecar_url}/sessions/{session_id}/messages"
        try:
            response = await client.post(url, json=payload)
        except httpx.HTTPError:
            _LOGGER.exception(
                "WhatsApp sidecar send transport error session_id=%s",
                session_id,
            )
            return
        if response.status_code >= 400:
            _LOGGER.warning(
                "WhatsApp sidecar send failed session_id=%s status=%s body=%s",
                session_id,
                response.status_code,
                response.text[:200],
            )
