"""HTTP client for the Hosted Official LINE Channel Hub delivery API (LH4).

Core owns *whether* a character has a semantically-worthwhile proactive message
and *what* it says; this client only carries the immutable envelope to the
Channel Hub, which owns *whether this channel can send it now* (active endpoint,
opt-in, quiet hours, quota — plan §4.4). Two internal endpoints are reached, both
behind the structured service credential (``caller=core``, ``audience=
yuralume-channel``):

* ``GET  /internal/v1/delivery-eligibility/{tenant}/{account}/{character}``
  (scope ``delivery:eligibility-read``) — non-authoritative cost preflight. The
  verdict lives in the response BODY (``eligible``), not the status code: the
  channel answers ``200`` for "no, this character is not opted in" too.
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

import json
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from kokoro_link.contracts.external_proactive import (
    ExternalProactiveConfigurationError,
    ExternalProactiveTransientError,
)
from kokoro_link.infrastructure.cloud.internal_service_auth import (
    outbound_headers,
)

_LOGGER = logging.getLogger(__name__)

# Accept-result discriminants — kept as module constants so the adapter maps
# them to the ledger lifecycle without importing httpx status internals.
ACCEPT_ACCEPTED = "accepted"
ACCEPT_NO_ENDPOINT = "no_endpoint"
ACCEPT_CONFLICT = "conflict"

# The channel's decline ``reason`` is a remote string that lands in our log on a
# per-character cadence; collapse and clamp it so a buggy or verbose peer cannot
# forge log lines or flood the log.
_REASON_MAX_CHARS = 120
_REASON_UNSPECIFIED = "unspecified"


class ChannelDeliveryTransientError(ExternalProactiveTransientError):
    """Retryable channel transport failure (429 / 5xx / network / timeout).

    The delivery is neither accepted nor terminally rejected. Callers must leave
    the pre-send ledger row ``pending`` (no ``settle``) so a later worker
    re-sends the SAME ``event_id``.
    """


class ChannelDeliveryConfigurationError(ExternalProactiveConfigurationError):
    """The Channel client cannot issue a valid request until config changes."""


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

        ``200`` means "the channel answered", NOT "yes" — the verdict is the
        body's ``eligible`` field (LH7-D). ``False`` therefore comes from either
        a ``200`` decline (e.g. this character is not opted in) or a ``404`` (no
        endpoint at all); both mean "do not spend decider/generation budget on
        this character now". Any other status, a network error, or a timeout
        raises :class:`ChannelDeliveryTransientError` so the adapter can answer
        "not eligible, but for a retryable reason" and cache it only briefly.
        """
        if not self._base_url:
            raise ChannelDeliveryConfigurationError("channel base URL is empty")
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
            return _verdict_from_body(response)
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
            raise ChannelDeliveryConfigurationError("channel base URL is empty")
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


def _verdict_from_body(response: httpx.Response) -> bool:
    """Read the eligibility verdict out of a ``200`` body (LH7-D).

    A missing / non-boolean ``eligible`` keeps the pre-LH7-D status-only
    semantics (``True``) instead of failing closed: during a mixed-version
    deployment (new Core, old Channel) a contract gap must not silently stop
    every proactive push. Arbitration re-checks every gate at send time and
    still drops what must be dropped, so the cost is wasted generation — not a
    wrong send — and the warning names the mismatch loudly.

    Identifiers stay OUT of these log lines on purpose — this module's logging
    discipline is "only the event id and transport status reach the log", and
    the eligibility path has neither. Correlation lives server-side: the same
    decline is recorded per character in the channel's delivery ledger.
    """
    body = _json_object(response)
    eligible = body.get("eligible") if body is not None else None
    if not isinstance(eligible, bool):
        _LOGGER.warning(
            "channel eligibility 200 without a usable 'eligible' field — "
            "assuming eligible; Core and Channel contract versions may be "
            "mismatched",
        )
        return True
    if eligible:
        return True
    _LOGGER.info(
        "channel eligibility declined reason=%s — skipping proactive "
        "generation for this tick",
        _reason_text(body),
    )
    return False


def _reason_text(body: dict[str, Any]) -> str:
    value = body.get("reason")
    if value is None:
        return _REASON_UNSPECIFIED
    text = " ".join(str(value).split())[:_REASON_MAX_CHARS]
    return text or _REASON_UNSPECIFIED


def _delivery_id(response: httpx.Response) -> str | None:
    body = _json_object(response)
    if body is None:
        return None
    value = body.get("delivery_id")
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _json_object(response: httpx.Response) -> dict[str, Any] | None:
    """Parse a response body into a JSON object, or ``None`` if unusable.

    Strict on purpose: the stdlib decoder accepts the non-standard ``NaN`` /
    ``Infinity`` constants by default (a body carrying them is not the
    contract's JSON), and an adversarially deep body raises ``RecursionError``
    out of the decoder. Both collapse to "unusable" here so every caller takes
    its documented degraded path instead of crashing the tick.
    """
    try:
        body = json.loads(response.text, parse_constant=_reject_json_constant)
    except (ValueError, RecursionError):
        return None
    return body if isinstance(body, dict) else None


def _reject_json_constant(value: str) -> Any:
    raise ValueError(f"non-standard JSON constant: {value}")
