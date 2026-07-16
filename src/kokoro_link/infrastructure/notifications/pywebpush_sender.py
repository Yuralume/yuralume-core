"""pywebpush-backed Web Push sender."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any

from kokoro_link.contracts.notifications import (
    WebPushPayload,
    WebPushSenderPort,
    WebPushSendResult,
)
from kokoro_link.domain.entities.web_push import (
    WebPushSubscription,
    validate_web_push_endpoint,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class WebPushVapidConfig:
    public_key: str = ""
    private_key: str = ""
    subject: str = "mailto:admin@example.invalid"
    # Store-and-forward retention handed to the push service. TTL=0 makes
    # delivery live-only (dropped when the device is offline/dozing);
    # a positive TTL lets the message be delivered on reconnect.
    ttl_seconds: int = 86400

    @property
    def configured(self) -> bool:
        return bool(self.public_key and self.private_key)


class NullWebPushSender(WebPushSenderPort):
    async def send(
        self,
        subscription: WebPushSubscription,
        payload: WebPushPayload,
    ) -> WebPushSendResult:
        _ = subscription, payload
        return WebPushSendResult.failure(error="web push is not configured")


class PyWebPushSender(WebPushSenderPort):
    def __init__(self, config: WebPushVapidConfig) -> None:
        self._config = config

    async def send(
        self,
        subscription: WebPushSubscription,
        payload: WebPushPayload,
    ) -> WebPushSendResult:
        if not self._config.configured:
            return WebPushSendResult.failure(error="web push is not configured")
        try:
            validate_web_push_endpoint(subscription.endpoint)
        except ValueError as exc:
            _LOGGER.warning(
                "web push endpoint rejected before send endpoint=%s error=%s",
                subscription.endpoint,
                exc,
            )
            return WebPushSendResult.failure(error=str(exc))
        try:
            return await asyncio.to_thread(
                self._send_blocking,
                subscription,
                payload,
            )
        except Exception as exc:  # pragma: no cover - defensive boundary
            status = _status_code_from_exception(exc)
            if status not in {404, 410}:
                _LOGGER.exception(
                    "web push send failed endpoint=%s status=%s",
                    subscription.endpoint,
                    status,
                )
            return WebPushSendResult.failure(
                status_code=status,
                error=str(exc),
            )

    def _send_blocking(
        self,
        subscription: WebPushSubscription,
        payload: WebPushPayload,
    ) -> WebPushSendResult:
        try:
            from pywebpush import WebPushException, webpush
        except ImportError as exc:
            return WebPushSendResult.failure(error=str(exc))

        subscription_info = {
            "endpoint": subscription.endpoint,
            "keys": {
                "p256dh": subscription.p256dh,
                "auth": subscription.auth,
            },
        }
        try:
            response = webpush(
                subscription_info=subscription_info,
                data=json.dumps(payload.as_json(), ensure_ascii=False),
                vapid_private_key=self._config.private_key,
                vapid_claims={"sub": self._config.subject},
                ttl=self._config.ttl_seconds,
            )
        except WebPushException as exc:
            status = _status_code_from_exception(exc)
            return WebPushSendResult.failure(
                status_code=status,
                error=str(exc),
            )
        status_code = int(getattr(response, "status_code", 201) or 201)
        if 200 <= status_code < 300:
            return WebPushSendResult.success(status_code=status_code)
        return WebPushSendResult.failure(
            status_code=status_code,
            error=f"unexpected push response {status_code}",
        )


def _status_code_from_exception(exc: BaseException) -> int | None:
    response: Any = getattr(exc, "response", None)
    if response is None:
        return None
    status = getattr(response, "status_code", None)
    if isinstance(status, int):
        return status
    return None
