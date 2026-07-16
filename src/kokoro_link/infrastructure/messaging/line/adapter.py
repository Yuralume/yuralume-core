"""LINE channel adapter — outbound side, reply-first with push fallback.

Cost model: LINE's reply API (answering a webhook event via its one-time
``replyToken``) is free, while every push *call* counts against the
official account's monthly message quota (200/month on the free plan —
LINE bills per call recipient, not per message object). Both endpoints
take up to 5 message objects per call, so a batched multi-bubble reply
flattens every bubble into message objects, rides the first 5 on the
free reply call, and packs any overflow into 5-object push calls.
Proactive sends carry no token and chunk straight onto push.

Fallback discipline: LLM generation can outlive the token's short
validity, so a reply rejected by LINE (4xx — expired / already used)
falls back to push and the message is never lost. Transport errors and
5xx leave the first chunk's delivery ambiguous, so we deliberately do
NOT re-send it — double-delivery is worse than the (rare, already-
logged) loss — while later chunks were never attempted and still push.

Stateless: credentials are threaded through ``OutboundMessage.credentials``
so one adapter instance handles every ``MessagingAccount`` on LINE.
"""

from __future__ import annotations

import enum
import logging
from collections.abc import Sequence

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
_REPLY_PATH = "/v2/bot/message/reply"
_REPLY_TOKEN_KEY = "reply_token"
_REQUEST_TIMEOUT_SECONDS = 15.0
_MAX_OBJECTS_PER_CALL = 5


class _ReplyOutcome(enum.Enum):
    DELIVERED = "delivered"
    REJECTED = "rejected"
    """LINE answered 4xx — definitively not delivered, push is safe."""
    AMBIGUOUS = "ambiguous"
    """Transport error / 5xx — delivery unknown, never re-send."""


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
        await self.send_many((message,))

    async def send_many(self, messages: Sequence[OutboundMessage]) -> None:
        """Deliver one logical reply's bubbles in as few calls as possible.

        Every bubble's message objects are flattened in order and cut
        into 5-object chunks (the per-call ceiling shared by reply and
        push). The first chunk rides the free reply call when the batch
        carries a ``replyToken`` — segmentation guarantees it sits on
        the first bubble — and every remaining chunk goes out as one
        push call, so N bubbles cost ``ceil(N/5)`` quota units at most
        instead of ``N - 1``.
        """
        batch = tuple(messages)
        for message in batch:
            if message.platform != Platform.LINE:
                raise ValueError(
                    f"LineAdapter cannot handle platform {message.platform.value}",
                )
        if not batch:
            return
        chat_ref = batch[0].chat_ref
        access_token = batch[0].credentials.get("channel_access_token", "")
        if not access_token:
            _LOGGER.warning(
                "LINE push skipped — missing channel_access_token for chat_ref=%s",
                chat_ref,
            )
            return

        objects = [
            obj
            for message in batch
            for obj in self._build_message_objects(message)
        ]
        if not objects:
            # Nothing to send — a call with zero message objects would
            # make LINE reject it with 400.
            _LOGGER.debug(
                "LINE push skipped — empty message for chat_ref=%s",
                chat_ref,
            )
            return
        chunks = [
            objects[start:start + _MAX_OBJECTS_PER_CALL]
            for start in range(0, len(objects), _MAX_OBJECTS_PER_CALL)
        ]

        headers = self._auth_headers(access_token)
        async with httpx.AsyncClient(
            transport=self._transport, timeout=_REQUEST_TIMEOUT_SECONDS,
        ) as client:
            remaining = chunks
            reply_token = batch[0].reply_context.get(_REPLY_TOKEN_KEY, "")
            if reply_token:
                outcome = await self._try_reply(
                    client,
                    reply_token=reply_token,
                    messages=chunks[0],
                    headers=headers,
                    chat_ref=chat_ref,
                )
                if outcome is not _ReplyOutcome.REJECTED:
                    # DELIVERED consumed the first chunk; AMBIGUOUS must
                    # not re-send it. Later chunks were never attempted,
                    # so pushing them cannot double-deliver.
                    remaining = chunks[1:]
            for chunk in remaining:
                await self._push(
                    client,
                    chat_ref=chat_ref,
                    messages=chunk,
                    headers=headers,
                )

    def _build_message_objects(self, message: OutboundMessage) -> list[dict]:
        """Translate one bubble's text + attachments into message objects.

        No per-call cap here — ``send_many`` chunks the flattened batch
        into 5-object calls, so nothing gets silently dropped anymore.
        """
        messages: list[dict] = []
        if message.text:
            messages.append({"type": "text", "text": message.text})
        for attachment in message.attachments:
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
        return messages

    async def _try_reply(
        self,
        client: httpx.AsyncClient,
        *,
        reply_token: str,
        messages: list[dict],
        headers: dict[str, str],
        chat_ref: str,
    ) -> _ReplyOutcome:
        """Answer the triggering webhook event on the free reply API.

        4xx → ``REJECTED`` (token expired / already used — LINE did not
        deliver, caller falls back to push so the message isn't lost).
        Transport error or 5xx → ``AMBIGUOUS`` (delivery unknown, caller
        must NOT push or the user may receive the reply twice).
        """
        url = f"{self._api_base}{_REPLY_PATH}"
        payload = {"replyToken": reply_token, "messages": messages}
        try:
            response = await client.post(url, json=payload, headers=headers)
        except httpx.HTTPError:
            _LOGGER.exception(
                "LINE reply transport error chat_ref=%s", chat_ref,
            )
            return _ReplyOutcome.AMBIGUOUS
        if response.status_code < 400:
            return _ReplyOutcome.DELIVERED
        if response.status_code < 500:
            _LOGGER.info(
                "LINE reply rejected chat_ref=%s status=%s body=%s — "
                "falling back to push",
                chat_ref, response.status_code, response.text[:200],
            )
            return _ReplyOutcome.REJECTED
        _LOGGER.warning(
            "LINE reply failed chat_ref=%s status=%s body=%s — delivery "
            "ambiguous, not retrying via push",
            chat_ref, response.status_code, response.text[:200],
        )
        return _ReplyOutcome.AMBIGUOUS

    async def _push(
        self,
        client: httpx.AsyncClient,
        *,
        chat_ref: str,
        messages: list[dict],
        headers: dict[str, str],
    ) -> None:
        url = f"{self._api_base}{_PUSH_PATH}"
        payload = {"to": chat_ref, "messages": messages}
        try:
            response = await client.post(url, json=payload, headers=headers)
        except httpx.HTTPError:
            _LOGGER.exception(
                "LINE push transport error chat_ref=%s", chat_ref,
            )
            return
        if response.status_code >= 400:
            _LOGGER.warning(
                "LINE push failed chat_ref=%s status=%s body=%s",
                chat_ref, response.status_code, response.text[:200],
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
