"""Telegram channel adapter — outbound side.

Stateless now: one adapter instance serves every ``MessagingAccount``
on Telegram. Credentials arrive per-call inside ``OutboundMessage`` so
the dispatcher can route different characters' bots through the same
adapter without any global state.

Images are uploaded as **multipart** rather than passing a URL. Reasons:

1. Telegram's URL-fetcher has a ~5s timeout and sometimes returns
   ``Bad Request: failed to get HTTP URL content`` even for URLs that
   resolve fine from a browser — their media fetchers use a different
   network path from webhook delivery, and slow DDNS / hairpin NAT on
   self-hosted setups hits this regularly.
2. Multipart works identically whether the URL is public, private,
   or behind weird network topologies, so long as *our* server can
   GET it. We control that path.

Trade-off: one extra GET per image (self-fetch before forwarding). For
ComfyUI portraits (~1–3 MB) that's negligible, and we sidestep an
entire class of delivery failures.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import PurePosixPath
from urllib.parse import urlparse

import httpx

from kokoro_link.contracts.messaging import (
    ChannelAdapterPort,
    OutboundAttachment,
    OutboundMessage,
)
from kokoro_link.domain.value_objects.platform import Platform
from kokoro_link.infrastructure.localization import localized_fallback_text
from kokoro_link.infrastructure.messaging.telegram.image_probe import (
    decide_photo_vs_document,
)

_LOGGER = logging.getLogger(__name__)
_DEFAULT_API_BASE = "https://api.telegram.org"
_REQUEST_TIMEOUT_SECONDS = 30.0
"""Longer than before (was 15s). Multipart uploads have to push the
image bytes to Telegram, so we need headroom on slower uplinks."""
_TG_CAPTION_LIMIT = 1024
"""Telegram's sendPhoto caption hard limit. Over this we split the
caption into a separate text message so nothing gets truncated."""
_MAX_PHOTO_BYTES = 10 * 1024 * 1024
"""Telegram sendPhoto hard cap is 10 MB. Over this we'd need
sendDocument. We log + drop rather than corrupting the send."""


@dataclass(frozen=True, slots=True)
class LocalImageFetchResult:
    handled: bool
    content: bytes | None = None


class TelegramAdapter(ChannelAdapterPort):
    def __init__(
        self,
        *,
        api_base: str = _DEFAULT_API_BASE,
        transport: httpx.AsyncBaseTransport | None = None,
        local_image_fetcher: (
            Callable[[str], Awaitable[LocalImageFetchResult | bytes | None]]
            | None
        ) = None,
    ) -> None:
        self._api_base = api_base.rstrip("/")
        self._transport = transport
        self._local_image_fetcher = local_image_fetcher

    @property
    def platform(self) -> Platform:
        return Platform.TELEGRAM

    async def send(self, message: OutboundMessage) -> None:
        if message.platform != Platform.TELEGRAM:
            raise ValueError(
                f"TelegramAdapter cannot handle platform {message.platform.value}",
            )
        bot_token = message.credentials.get("bot_token", "")
        if not bot_token:
            _LOGGER.warning(
                "Telegram send skipped — missing bot_token for chat_ref=%s",
                message.chat_ref,
            )
            return

        images = [a for a in message.attachments if a.kind == "image"]
        others = [a for a in message.attachments if a.kind != "image"]

        async with httpx.AsyncClient(
            transport=self._transport, timeout=_REQUEST_TIMEOUT_SECONDS,
        ) as client:
            # Decide how to distribute the text. If we have images and
            # the text fits in a caption, attach it to the *first*
            # photo — single notification, single visual unit. If the
            # text is too long, we send it as its own sendMessage first
            # so the recipient can't miss it while scrolling the album.
            text = message.text or ""
            caption_for_first_photo: str | None = None
            leftover_text = text
            if images and text and len(text) <= _TG_CAPTION_LIMIT:
                caption_for_first_photo = text
                leftover_text = ""

            if leftover_text:
                await self._post_send_message(
                    client, bot_token=bot_token,
                    chat_ref=message.chat_ref, text=leftover_text,
                )

            for index, image in enumerate(images):
                caption = (
                    caption_for_first_photo if index == 0 else image.caption
                )
                await self._post_send_photo(
                    client, bot_token=bot_token,
                    chat_ref=message.chat_ref, image=image, caption=caption,
                )

            # Non-image attachments: fall through to a plain text note
            # with the URL. Telegram sendDocument needs the file too, so
            # we keep the simple path for now.
            if others:
                lines = [
                    localized_fallback_text(
                        "channel.telegram.other_attachments",
                        message.locale,
                    ),
                ]
                for att in others:
                    lines.append(f"- {att.caption or att.url}")
                await self._post_send_message(
                    client, bot_token=bot_token,
                    chat_ref=message.chat_ref, text="\n".join(lines),
                )

    async def _post_send_message(
        self,
        client: httpx.AsyncClient,
        *,
        bot_token: str,
        chat_ref: str,
        text: str,
    ) -> None:
        url = f"{self._api_base}/bot{bot_token}/sendMessage"
        payload = {"chat_id": chat_ref, "text": text}
        try:
            response = await client.post(url, json=payload)
        except httpx.HTTPError:
            _LOGGER.exception(
                "Telegram sendMessage transport error chat_ref=%s", chat_ref,
            )
            return
        if response.status_code >= 400:
            _LOGGER.warning(
                "Telegram sendMessage failed chat_ref=%s status=%s body=%s",
                chat_ref, response.status_code, response.text[:200],
            )

    async def _post_send_photo(
        self,
        client: httpx.AsyncClient,
        *,
        bot_token: str,
        chat_ref: str,
        image: OutboundAttachment,
        caption: str | None,
    ) -> None:
        # Step 1: Resolve image bytes. Core-owned media can be read from
        # object storage directly, avoiding public URL round-trips
        # entirely; external CDN URLs still fall back to HTTP GET.
        image_bytes = await self._fetch_image_bytes(client, chat_ref, image.url)
        if image_bytes is None:
            return  # already logged
        if len(image_bytes) > _MAX_PHOTO_BYTES:
            _LOGGER.warning(
                "Telegram sendPhoto skipped chat_ref=%s url=%s — %d bytes "
                "exceeds 10 MB sendPhoto limit",
                chat_ref, image.url, len(image_bytes),
            )
            return

        content_type = _resolve_content_type(image)
        filename = _guess_filename(image.url, content_type)

        # Step 2: pre-flight dimension check. Telegram sendPhoto rejects
        # extreme aspect ratios and very large images with an opaque 400;
        # degrading to sendDocument keeps delivery reliable for edge
        # cases (super-tall infographics, high-res ComfyUI outputs).
        decision = decide_photo_vs_document(
            image_bytes,
            log_context=f"chat_ref={chat_ref} url={image.url}",
        )
        if decision == "photo":
            endpoint = "sendPhoto"
            file_field = "photo"
        else:
            endpoint = "sendDocument"
            file_field = "document"

        # Step 3: multipart POST. ``data=`` carries text fields,
        # ``files=`` carries the binary. httpx builds the boundary.
        url = f"{self._api_base}/bot{bot_token}/{endpoint}"
        data: dict[str, str] = {"chat_id": chat_ref}
        if caption:
            data["caption"] = caption
        files = {file_field: (filename, image_bytes, content_type)}
        try:
            response = await client.post(url, data=data, files=files)
        except httpx.HTTPError:
            _LOGGER.exception(
                "Telegram %s transport error chat_ref=%s url=%s",
                endpoint, chat_ref, image.url,
            )
            return
        if response.status_code >= 400:
            _LOGGER.warning(
                "Telegram %s failed chat_ref=%s url=%s status=%s body=%s",
                endpoint, chat_ref, image.url,
                response.status_code, response.text[:200],
            )

    async def _fetch_image_bytes(
        self,
        client: httpx.AsyncClient,
        chat_ref: str,
        url: str,
    ) -> bytes | None:
        """GET the image from ``url``; return bytes or ``None`` on error.

        Logs every failure mode separately so ops can tell the difference
        between "our server can't reach the upload URL" (config bug) and
        "remote server returned 404" (dangling attachment).
        """
        if self._local_image_fetcher is not None:
            try:
                result = await self._local_image_fetcher(url)
            except Exception:
                _LOGGER.exception(
                    "Telegram local image fetcher crashed chat_ref=%s url=%s",
                    chat_ref, url,
                )
                result = LocalImageFetchResult(handled=True)
            if isinstance(result, LocalImageFetchResult):
                if result.handled:
                    return result.content
            elif result is not None:
                return result

        try:
            response = await client.get(url, follow_redirects=True)
        except httpx.HTTPError:
            _LOGGER.exception(
                "Telegram image fetch transport error chat_ref=%s url=%s",
                chat_ref, url,
            )
            return None
        if response.status_code >= 400:
            _LOGGER.warning(
                "Telegram image fetch failed chat_ref=%s url=%s status=%s",
                chat_ref, url, response.status_code,
            )
            return None
        return response.content

    async def set_webhook(
        self,
        *,
        bot_token: str,
        webhook_url: str,
        secret_token: str = "",
    ) -> dict:
        """Register the webhook URL with Telegram Bot API.

        Returns the raw JSON response from Telegram, which looks like
        ``{"ok": true, "result": true, "description": "..."}`` on
        success. Callers inspect ``ok`` to decide success / failure.
        Transport errors come back as ``{"ok": false, "error": "..."}``.
        """
        url = f"{self._api_base}/bot{bot_token}/setWebhook"
        payload: dict[str, str] = {"url": webhook_url}
        if secret_token:
            payload["secret_token"] = secret_token
        return await self._post_json(url, payload)

    async def get_webhook_info(self, *, bot_token: str) -> dict:
        """Return ``getWebhookInfo`` raw response or ``{"ok": false, ...}``."""
        url = f"{self._api_base}/bot{bot_token}/getWebhookInfo"
        return await self._get_json(url)

    async def delete_webhook(
        self,
        *,
        bot_token: str,
        drop_pending_updates: bool = False,
    ) -> dict:
        """Remove Telegram webhook so ``getUpdates`` polling can run."""
        url = f"{self._api_base}/bot{bot_token}/deleteWebhook"
        return await self._post_json(
            url, {"drop_pending_updates": drop_pending_updates},
        )

    async def get_updates(
        self,
        *,
        bot_token: str,
        offset: int | None = None,
        timeout_seconds: int = 25,
        limit: int = 100,
    ) -> dict:
        """Long-poll Telegram Bot API updates for polling delivery mode."""
        url = f"{self._api_base}/bot{bot_token}/getUpdates"
        payload: dict[str, object] = {
            "timeout": timeout_seconds,
            "limit": limit,
            "allowed_updates": ["message"],
        }
        if offset is not None:
            payload["offset"] = offset
        return await self._post_json(url, payload)

    async def _post_json(self, url: str, payload: dict) -> dict:
        async with httpx.AsyncClient(
            transport=self._transport, timeout=_REQUEST_TIMEOUT_SECONDS,
        ) as client:
            try:
                response = await client.post(url, json=payload)
            except httpx.HTTPError as exc:
                return {"ok": False, "error": str(exc)}
        return _safe_json(response)

    async def _get_json(self, url: str) -> dict:
        async with httpx.AsyncClient(
            transport=self._transport, timeout=_REQUEST_TIMEOUT_SECONDS,
        ) as client:
            try:
                response = await client.get(url)
            except httpx.HTTPError as exc:
                return {"ok": False, "error": str(exc)}
        return _safe_json(response)


_EXT_TO_CONTENT_TYPE = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}
_CONTENT_TYPE_TO_FILENAME = {
    "image/png": "photo.png",
    "image/jpeg": "photo.jpg",
    "image/webp": "photo.webp",
    "image/gif": "photo.gif",
}


def _resolve_content_type(image: OutboundAttachment) -> str:
    """Prefer the DTO's declared mime; fall back to URL extension.

    We deliberately do not trust ``Content-Type`` from the fetch
    response — some static servers mis-label PNGs as
    ``application/octet-stream``, which Telegram rejects. The attachment
    mime comes from our own code (ComfyUI tool, character images) so
    it's reliable.
    """
    if image.mime_type and image.mime_type.startswith("image/"):
        return image.mime_type
    suffix = PurePosixPath(urlparse(image.url).path).suffix.lower()
    return _EXT_TO_CONTENT_TYPE.get(suffix, "image/png")


def _guess_filename(url: str, content_type: str) -> str:
    """Derive a filename for the multipart field.

    Telegram doesn't actually require a specific name but an empty or
    malformed one can trigger quirky errors on some SDKs; we aim for
    ``<basename>.<ext>`` from the URL, falling back to a generic name
    keyed on content-type.
    """
    name = PurePosixPath(urlparse(url).path).name
    if name and "." in name:
        return name
    return _CONTENT_TYPE_TO_FILENAME.get(content_type, "photo.bin")


def _safe_json(response: httpx.Response) -> dict:
    try:
        data = response.json()
    except ValueError:
        return {
            "ok": False,
            "error": f"non-JSON response status={response.status_code}",
        }
    if not isinstance(data, dict):
        return {"ok": False, "error": "unexpected response shape"}
    return data
