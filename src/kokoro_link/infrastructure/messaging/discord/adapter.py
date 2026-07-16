"""Discord channel adapter — outbound REST side."""

from __future__ import annotations

import logging
from collections.abc import Sequence

import httpx

from kokoro_link.contracts.messaging import ChannelAdapterPort, OutboundMessage
from kokoro_link.domain.value_objects.platform import Platform

_LOGGER = logging.getLogger(__name__)
_DEFAULT_API_BASE = "https://discord.com/api/v10"
_REQUEST_TIMEOUT_SECONDS = 15.0
_CONTENT_LIMIT = 2000


class DiscordAdapter(ChannelAdapterPort):
    def __init__(
        self,
        *,
        api_base: str = _DEFAULT_API_BASE,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_base = api_base.rstrip("/")
        self._transport = transport

    @property
    def platform(self) -> Platform:
        return Platform.DISCORD

    async def send_many(self, messages: Sequence[OutboundMessage]) -> None:
        # Discord has no multi-message endpoint — deliver the batch
        # sequentially, identical to the old per-bubble loop.
        for message in messages:
            await self.send(message)

    async def send(self, message: OutboundMessage) -> None:
        if message.platform != Platform.DISCORD:
            raise ValueError(
                f"DiscordAdapter cannot handle platform {message.platform.value}",
            )
        bot_token = message.credentials.get("bot_token", "")
        if not bot_token:
            _LOGGER.warning(
                "Discord send skipped — missing bot_token for chat_ref=%s",
                message.chat_ref,
            )
            return

        content = _append_attachment_urls(message)
        if not content.strip():
            _LOGGER.debug(
                "Discord send skipped — empty message for chat_ref=%s",
                message.chat_ref,
            )
            return

        async with httpx.AsyncClient(
            transport=self._transport,
            timeout=_REQUEST_TIMEOUT_SECONDS,
            headers={
                "Authorization": f"Bot {bot_token}",
                "Content-Type": "application/json",
            },
        ) as client:
            for chunk in _split_content(content):
                await self._post_message(
                    client,
                    channel_id=message.chat_ref,
                    content=chunk,
                )

    async def _post_message(
        self,
        client: httpx.AsyncClient,
        *,
        channel_id: str,
        content: str,
    ) -> None:
        url = f"{self._api_base}/channels/{channel_id}/messages"
        payload = {
            "content": content,
            "allowed_mentions": {"parse": []},
        }
        try:
            response = await client.post(url, json=payload)
        except httpx.HTTPError:
            _LOGGER.exception(
                "Discord create message transport error channel_id=%s",
                channel_id,
            )
            return
        if response.status_code >= 400:
            _LOGGER.warning(
                "Discord create message failed channel_id=%s status=%s body=%s",
                channel_id,
                response.status_code,
                response.text[:200],
            )


def _append_attachment_urls(message: OutboundMessage) -> str:
    lines: list[str] = []
    if message.text:
        lines.append(message.text)
    for attachment in message.attachments:
        label = (
            f"{attachment.caption}: {attachment.url}"
            if attachment.caption else attachment.url
        )
        lines.append(f"Attachment: {label}")
    return "\n".join(lines)


def _split_content(content: str) -> list[str]:
    if len(content) <= _CONTENT_LIMIT:
        return [content]

    chunks: list[str] = []
    remaining = content
    while remaining:
        if len(remaining) <= _CONTENT_LIMIT:
            chunks.append(remaining)
            break
        split_at = remaining.rfind("\n", 0, _CONTENT_LIMIT)
        if split_at < 1:
            split_at = _CONTENT_LIMIT
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:].lstrip("\n")
    return chunks
