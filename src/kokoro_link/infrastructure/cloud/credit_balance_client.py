"""HTTP client for the User service internal credit-balance endpoint."""

from __future__ import annotations

from math import isfinite

import httpx

from kokoro_link.contracts.cloud_credit_balance import (
    CloudCreditBalance,
    CreditBalancePort,
    CreditBalanceUnavailable,
)
from kokoro_link.infrastructure.cloud.internal_service_auth import (
    outbound_headers,
)


class CreditBalanceClient(CreditBalancePort):
    """Fetches ``GET /internal/v1/credits/balance?tenant_id=<uuid>``.

    Shaped like :class:`TierRuntimeProfileClient`: same versioned
    caller/audience/scope service credential (``caller=core``,
    ``audience=yuralume-user``, scope ``credits:read``), same per-call client
    construction and timeout. Unlike the tier profile there is no 404 case to
    model — an unknown tenant answers 200 with an all-zero balance — so every
    non-2xx, malformed body, or transport failure raises
    ``CreditBalanceUnavailable`` and the caching service decides whether to
    serve last-known-good.

    Unlike its siblings there is deliberately **no legacy ``X-Internal-Token``
    fallback**: Cloud marks the balance endpoint legacy-ineligible, so a legacy
    token would answer 401 and the only thing a knob for it could do is invite
    a misconfiguration that reads as "the wallet is unavailable".
    """

    _PATH = "/internal/v1/credits/balance"

    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float = 5.0,
        internal_credential: str = "",
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._internal_credential = (internal_credential or "").strip()

    async def fetch(self, tenant_id: str) -> CloudCreditBalance:
        if not self._base_url:
            raise CreditBalanceUnavailable("cloud user service URL is empty")
        cleaned = (tenant_id or "").strip()
        if not cleaned:
            raise CreditBalanceUnavailable("tenant id is empty")
        headers = outbound_headers(self._internal_credential)
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout_seconds,
            ) as client:
                response = await client.get(
                    self._PATH,
                    params={"tenant_id": cleaned},
                    headers=headers,
                )
        except httpx.HTTPError as exc:
            raise CreditBalanceUnavailable(
                "cloud user service unavailable",
            ) from exc
        if response.status_code >= 400:
            raise CreditBalanceUnavailable(
                f"cloud user service returned {response.status_code}",
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise CreditBalanceUnavailable(
                "credit balance response is not valid JSON",
            ) from exc
        if not isinstance(payload, dict):
            raise CreditBalanceUnavailable(
                "credit balance response is not a JSON object",
            )
        gift = _require_amount(payload, "gift_cr")
        purchased = _require_amount(payload, "purchased_cr")
        total = _require_amount(payload, "total_cr")
        if abs(total - (gift + purchased)) > _TOTAL_TOLERANCE_CR:
            raise CreditBalanceUnavailable(
                "credit balance total contradicts its pools",
            )
        return CloudCreditBalance(
            gift_cr=gift,
            purchased_cr=purchased,
            total_cr=total,
            next_gift_expiry=_as_optional_text(payload.get("next_gift_expiry")),
        )


_TOTAL_TOLERANCE_CR = 0.01
"""Each field is rounded independently upstream, so sub-cent drift between
``total`` and ``gift + purchased`` is normal. Anything larger means we are not
looking at one coherent ledger row."""


def _require_amount(payload: dict, field: str) -> float:
    """Read one credit amount, or refuse the whole snapshot.

    The upstream ledger serialises ``BigDecimal``; depending on the Jackson
    configuration that arrives as a number or a quoted decimal, so both are
    accepted. What is **not** accepted is a missing, null, non-finite or
    unparsable field: coercing those to ``0`` produced a structurally valid
    snapshot that the cache then stored as fresh, and the player was shown a
    confident "0 螢火" for a wallet that was never read. A refusal routes to
    the existing stale / 503 path, which says "we do not know" instead.
    """
    if field not in payload:
        raise CreditBalanceUnavailable(
            f"credit balance response is missing {field}",
        )
    raw = payload[field]
    if isinstance(raw, bool):
        value = None
    elif isinstance(raw, (int, float)):
        value = float(raw)
    elif isinstance(raw, str):
        try:
            value = float(raw.strip())
        except ValueError:
            value = None
    else:
        value = None
    if value is None or not isfinite(value):
        raise CreditBalanceUnavailable(
            f"credit balance field {field} is not a usable amount",
        )
    return value


def _as_optional_text(raw: object) -> str | None:
    if not isinstance(raw, str):
        return None
    return raw.strip() or None
