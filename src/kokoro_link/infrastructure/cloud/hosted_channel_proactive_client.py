"""HTTP client for the Hosted Official LINE Channel Hub delivery API (LH4).

Core owns *whether* a character has a semantically-worthwhile proactive message
and *what* it says; this client only carries the immutable envelope to the
Channel Hub, which owns *whether this channel can send it now* (active endpoint,
opt-in, quiet hours, quota — plan §4.4). Two internal endpoints are reached, both
behind the structured service credential (``caller=core``, ``audience=
yuralume-channel``):

* ``GET  /internal/v1/delivery-eligibility/{tenant}/{account}/{character}``
  (scope ``delivery:eligibility-read``) — non-authoritative cost preflight.
* ``POST /internal/v1/deliveries`` (scope ``delivery:create``) — durable accept
  of an :class:`ExternalDeliveryEnvelope`; ``202`` (or an idempotent replay of
  the same ``event_id``) means transport-accepted.

Transport-classification contract (mapped to ledger lifecycle by the adapter):

* ``2xx`` accept → :class:`ChannelAcceptResult` ``ACCEPTED`` with the channel's
  ``delivery_id``.
* ``404`` → ``NO_ENDPOINT`` (terminal — nowhere to deliver).
* ``409`` → ``CONFLICT`` (terminal — same ``event_id`` reused with a different
  payload hash).
* ``429`` / ``5xx`` / network / timeout → :class:`ChannelDeliveryTransientError`
  (retryable — the caller leaves the ledger row ``pending`` and re-sends the
  same event later).

Timeouts are bounded (eligibility 5s, delivery 10s) and message content is never
logged — only the ``event_id`` and transport status reach the log.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from kokoro_link.infrastructure.cloud.internal_service_auth import (
    outbound_headers,
)

# Accept-result discriminants — kept as module constants so the adapter maps
# them to the ledger lifecycle without importing httpx status internals.
ACCEPT_ACCEPTED = "accepted"
ACCEPT_NO_ENDPOINT = "no_endpoint"
ACCEPT_CONFLICT = "conflict"


class ChannelDeliveryTransientError(Exception):
    """Retryable channel transport failure (429 / 5xx / network / timeout).

    The delivery is neither accepted nor terminally rejected. Callers must leave
    the pre-send ledger row ``pending`` (no ``settle``) so a later worker
    re-sends the SAME ``event_id``.
    """


@dataclass(frozen=True, slots=True)
class ChannelAcceptResult:
    """Terminal-or-accepted outcome of :meth:`accept_delivery`."""

    status: str
    delivery_id: str | None = None


class HostedChannelProactiveClient:
    def __init__(
        self,
        *,
        base_url: str,
        service_credential: str,
        eligibility_timeout_seconds: float = 5.0,
        delivery_timeout_seconds: float = 10.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._service_credential = service_credential.strip()
        self._eligibility_timeout = eligibility_timeout_seconds
        self._delivery_timeout = delivery_timeout_seconds

    async def get_eligibility(
        self,
        *,
        tenant_id: str,
        account_id: str,
        character_id: str,
    ) -> bool:
        """Non-authoritative cost preflight.

        Returns ``True`` (``200`` — an active endpoint that opted this character
        in) or ``False`` (``404`` — no endpoint). Any other status, a network
        error, or a timeout raises :class:`ChannelDeliveryTransientError` so the
        adapter can answer "not eligible, but for a retryable reason" and cache
        it only briefly.
        """
        if not self._base_url:
            raise ChannelDeliveryTransientError("channel base URL is empty")
        path = (
            "/internal/v1/delivery-eligibility/"
            f"{tenant_id}/{account_id}/{character_id}"
        )
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._eligibility_timeout,
            ) as client:
                response = await client.get(path, headers=self._headers())
        except httpx.HTTPError as exc:
            raise ChannelDeliveryTransientError(
                "channel eligibility request failed",
            ) from exc
        if response.status_code == 200:
            return True
        if response.status_code == 404:
            return False
        raise ChannelDeliveryTransientError(
            f"channel eligibility returned {response.status_code}",
        )

    async def accept_delivery(
        self, envelope_payload: dict[str, Any],
    ) -> ChannelAcceptResult:
        """Durably hand the wire envelope to the channel.

        ``2xx`` → :data:`ACCEPT_ACCEPTED` (fresh queue or idempotent replay of
        the same ``event_id``). ``404`` → :data:`ACCEPT_NO_ENDPOINT`. ``409`` →
        :data:`ACCEPT_CONFLICT`. ``429`` / ``5xx`` / network / timeout raise
        :class:`ChannelDeliveryTransientError`.
        """
        if not self._base_url:
            raise ChannelDeliveryTransientError("channel base URL is empty")
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._delivery_timeout,
            ) as client:
                response = await client.post(
                    "/internal/v1/deliveries",
                    json=envelope_payload,
                    headers=self._headers(),
                )
        except httpx.HTTPError as exc:
            raise ChannelDeliveryTransientError(
                "channel delivery request failed",
            ) from exc
        status = response.status_code
        if 200 <= status < 300:
            return ChannelAcceptResult(
                status=ACCEPT_ACCEPTED,
                delivery_id=_delivery_id(response),
            )
        if status == 404:
            return ChannelAcceptResult(status=ACCEPT_NO_ENDPOINT)
        if status == 409:
            return ChannelAcceptResult(status=ACCEPT_CONFLICT)
        raise ChannelDeliveryTransientError(
            f"channel delivery returned {status}",
        )

    def _headers(self) -> dict[str, str]:
        return outbound_headers(self._service_credential)


def _delivery_id(response: httpx.Response) -> str | None:
    try:
        body = response.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    value = body.get("delivery_id")
    if value is None:
        return None
    text = str(value).strip()
    return text or None
