"""LINE channel adapter — outbound side via the push API.

Stateless: credentials are threaded through ``OutboundMessage.credentials``
so one adapter instance handles every ``MessagingAccount`` on LINE.
"""

from __future__ import annotations

import logging

import httpx

from kokoro_link.contracts.messaging import ChannelAdapterPort, OutboundMessage
from kokoro_link.domain.value_objects.platform import Platform
from kokoro_link.infrastructure.localization import localized_fallback_text
from kokoro_link.infrastructure.messaging.line.url_validation import (
    LineUrlValidationError,
    validate_line_image_url,
)

_LOGGER = logging.getLogger(__name__)
_DEFAULT_API_BASE = "https://api.line.me"
_PUSH_PATH = "/v2/bot/message/push"
_REQUEST_TIMEOUT_SECONDS = 15.0


class LineAdapter(ChannelAdapterPort):
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
        return Platform.LINE

    async def send(self, message: OutboundMessage) -> None:
        if message.platform != Platform.LINE:
            raise ValueError(
                f"LineAdapter cannot handle platform {message.platform.value}",
            )
        access_token = message.credentials.get("channel_access_token", "")
        if not access_token:
            _LOGGER.warning(
                "LINE push skipped — missing channel_access_token for chat_ref=%s",
                message.chat_ref,
            )
            return

        url = f"{self._api_base}{_PUSH_PATH}"
        # LINE push supports up to 5 message objects per call. We pack
        # the text + up to 4 images in one batch to minimise round
        # trips and keep them in a single notification.
        messages: list[dict] = []
        if message.text:
            messages.append({"type": "text", "text": message.text})
        for attachment in message.attachments:
            if len(messages) >= 5:
                break
            if attachment.kind == "image":
                # Pre-flight URL validation. LINE rejects non-https,
                # too-long URLs, and non-JPEG/PNG with an opaque 400;
                # running the cheap checks here gives ops a readable
                # reason in the log and saves a round trip.
                try:
                    validate_line_image_url(attachment.url)
                except LineUrlValidationError as err:
                    _LOGGER.warning(
                        "LINE image attachment skipped chat_ref=%s reason=%s url=%s",
                        message.chat_ref, err.reason, err.url,
                    )
                    # Fall back to a text note so the user still gets
                    # *something* pointing at the asset rather than
                    # silent drop.
                    label = attachment.caption or attachment.url
                    messages.append(
                        {
                            "type": "text",
                            "text": localized_fallback_text(
                                "channel.line.attachment_url_invalid",
                                message.locale,
                                label=label,
                            ),
                        },
                    )
                    continue
                # LINE wants both originalContentUrl and previewImageUrl.
                # We reuse the same URL for both — splitting is a later
                # optimisation (would need the tool to emit thumbnails).
                messages.append(
                    {
                        "type": "image",
                        "originalContentUrl": attachment.url,
                        "previewImageUrl": attachment.url,
                    },
                )
            else:
                # Unsupported attachment kind → degrade to a text line
                # containing the URL so the recipient isn't left guessing.
                label = attachment.caption or attachment.url
                messages.append(
                    {
                        "type": "text",
                        "text": localized_fallback_text(
                            "channel.line.attachment_label",
                            message.locale,
                            label=label,
                        ),
                    },
                )
        if not messages:
            # Nothing to send — outbound with empty text + empty
            # attachments would make LINE reject the call with 400.
            _LOGGER.debug(
                "LINE push skipped — empty message for chat_ref=%s",
                message.chat_ref,
            )
            return
        payload = {
            "to": message.chat_ref,
            "messages": messages,
        }
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(
            transport=self._transport, timeout=_REQUEST_TIMEOUT_SECONDS,
        ) as client:
            try:
                response = await client.post(url, json=payload, headers=headers)
            except httpx.HTTPError:
                _LOGGER.exception(
                    "LINE push transport error chat_ref=%s", message.chat_ref,
                )
                return
        if response.status_code >= 400:
            _LOGGER.warning(
                "LINE push failed chat_ref=%s status=%s body=%s",
                message.chat_ref, response.status_code, response.text[:200],
            )

    async def set_webhook_endpoint(
        self, *, channel_access_token: str, webhook_url: str,
    ) -> dict:
        """Point the channel's Messaging API webhook at ``webhook_url``.

        Returns ``{"ok": True}`` on success, ``{"ok": False, "error": ...}``
        otherwise. Uses LINE's
        ``PUT /v2/bot/channel/webhook/endpoint`` endpoint.
        """
        url = f"{self._api_base}/v2/bot/channel/webhook/endpoint"
        headers = self._auth_headers(channel_access_token)
        async with httpx.AsyncClient(
            transport=self._transport, timeout=_REQUEST_TIMEOUT_SECONDS,
        ) as client:
            try:
                response = await client.put(
                    url, json={"endpoint": webhook_url}, headers=headers,
                )
            except httpx.HTTPError as exc:
                return {"ok": False, "error": str(exc)}
        if response.status_code >= 400:
            return {
                "ok": False,
                "error": f"status={response.status_code} body={response.text[:200]}",
            }
        return {"ok": True}

    async def get_webhook_endpoint(
        self, *, channel_access_token: str,
    ) -> dict:
        """Read back the webhook the channel is currently pointing at."""
        url = f"{self._api_base}/v2/bot/channel/webhook/endpoint"
        headers = self._auth_headers(channel_access_token)
        async with httpx.AsyncClient(
            transport=self._transport, timeout=_REQUEST_TIMEOUT_SECONDS,
        ) as client:
            try:
                response = await client.get(url, headers=headers)
            except httpx.HTTPError as exc:
                return {"ok": False, "error": str(exc)}
        if response.status_code >= 400:
            return {
                "ok": False,
                "error": f"status={response.status_code} body={response.text[:200]}",
            }
        try:
            data = response.json()
        except ValueError:
            return {"ok": False, "error": "non-JSON response"}
        if not isinstance(data, dict):
            return {"ok": False, "error": "unexpected response shape"}
        return {"ok": True, **data}

    @staticmethod
    def _auth_headers(token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
