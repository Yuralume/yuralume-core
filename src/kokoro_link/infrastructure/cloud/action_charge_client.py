"""HTTP client for the User service internal action-charge endpoints (AP2).

Shaped like :class:`~kokoro_link.infrastructure.cloud.credit_balance_client.CreditBalanceClient`
— same versioned service credential (``caller=core``, ``audience=yuralume-user``),
same per-call client construction and timeout, same "no legacy
``X-Internal-Token`` fallback" stance. The scope differs: charging needs
``credits:charge``, which the balance read deliberately does not carry.

Three outcomes, three shapes:

* **2xx** → an :class:`ActionCharge`. ``no_charge=true`` (no active price for
  this ``(tier, action_key)``) is a *success*: the User service alerts and
  writes no ledger row, and the player keeps playing.
* **402** → :class:`~kokoro_link.infrastructure.llm.cloud_refusal.ExpectedCloudRefusal`
  with code ``insufficient_credits`` so the existing U4 guard turns it into the
  structured 402 every foreground route already knows how to render.
* **409** → :class:`ActionPriceChanged`. The price moved after Core quoted it,
  so nothing was reserved and the player must re-confirm the new number rather
  than be charged one they never saw.
* **anything else** → :class:`ActionChargeUnavailable`; the billing service
  runs the action uncharged rather than blocking play.
"""

from __future__ import annotations

import json
from math import isfinite

import httpx

from kokoro_link.contracts.cloud_action_billing import (
    ACTION_KIND_USER_ACTION,
    PRICE_CHANGED_CODE,
    ActionCharge,
    ActionChargePort,
    ActionChargeUnavailable,
    ActionPriceChanged,
)
from kokoro_link.infrastructure.cloud.internal_service_auth import (
    outbound_headers,
)
from kokoro_link.infrastructure.llm.cloud_refusal import (
    INSUFFICIENT_CREDITS_CODE,
    ExpectedCloudRefusal,
    refusal_summary,
)

_CHARGE_PATH = "/internal/v1/credits/actions/charge"
_ACTION_PATH = "/internal/v1/credits/actions"
_INSUFFICIENT_CREDITS_STATUS = 402
_PRICE_CHANGED_STATUS = 409
#: One retry, not a backoff loop: the caller is a player waiting on a reply, so
#: the budget is "survive a single dropped connection", not "ride out an
#: outage" — that case is already handled by running the action uncharged.
_TRANSPORT_ATTEMPTS = 2
_FALLBACK_REFUSAL_MESSAGE = "insufficient credits for this action"


class ActionChargeClient(ActionChargePort):
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

    async def charge(
        self,
        *,
        tenant_id: str,
        action_key: str,
        interaction_id: str,
        action_kind: str = ACTION_KIND_USER_ACTION,
        expected_price_cr: float | None = None,
    ) -> ActionCharge:
        tenant = (tenant_id or "").strip()
        action = (action_key or "").strip()
        interaction = (interaction_id or "").strip()
        if not (tenant and action and interaction):
            raise ActionChargeUnavailable(
                "action charge requires tenant, action key and interaction id",
            )
        payload: dict = {
            "tenant_id": tenant,
            "action_key": action,
            "interaction_id": interaction,
            "action_kind": action_kind,
        }
        if expected_price_cr is not None:
            # Omitted, never sent as 0.0: "Core quoted nothing" and "Core
            # quoted free" are different claims, and the second one would bind
            # the charge to a price the back office may never have published.
            payload["expected_price_cr"] = float(expected_price_cr)
        response = await self._post(_CHARGE_PATH, payload=payload)
        if response.status_code == _INSUFFICIENT_CREDITS_STATUS:
            raise _insufficient_credits(response)
        if response.status_code == _PRICE_CHANGED_STATUS:
            raise _price_changed(
                response, action_key=action, expected_price_cr=expected_price_cr,
            )
        if response.status_code >= 400:
            raise ActionChargeUnavailable(
                f"cloud user service returned {response.status_code}",
            )
        return _parse_charge(response)

    async def settle(self, charge_id: str) -> None:
        await self._close(charge_id, "settle")

    async def release(
        self, charge_id: str, *, settle_if_probed: bool = False,
    ) -> None:
        """Refund the reservation, or let the ledger decide when Core cannot.

        ``settle_if_probed`` is sent as a query parameter only when it is
        true: Core knows nothing about this charge's covered calls (a handle
        rebuilt from job params after a restart), so the User service resolves
        it from its own ``covered_probe_at`` and answers which way it went.
        Omitting the parameter keeps the request byte-identical to the ordinary
        refund.
        """
        await self._close(
            charge_id,
            "release",
            params={"settle_if_probed": "true"} if settle_if_probed else None,
        )

    async def _close(
        self,
        charge_id: str,
        verb: str,
        *,
        params: dict[str, str] | None = None,
    ) -> None:
        cleaned = (charge_id or "").strip()
        if not cleaned:
            raise ActionChargeUnavailable(f"{verb} requires a charge id")
        response = await self._post(
            f"{_ACTION_PATH}/{cleaned}/{verb}",
            payload={},
            params=params,
        )
        if response.status_code >= 400:
            raise ActionChargeUnavailable(
                f"action {verb} returned {response.status_code}",
            )

    async def _post(
        self,
        path: str,
        *,
        payload: dict,
        params: dict[str, str] | None = None,
    ) -> httpx.Response:
        """POST once, retrying a transport failure exactly once.

        A timeout is the dangerous answer, not the clean one: the transaction
        on the far side may well have committed, which would leave the player
        debited for a charge Core never learned about — money gone until the
        stale-reservation sweeper runs, plus a whole turn billed again per
        call. Every endpoint here is idempotent (``charge`` on the Core-owned
        ``interaction_id``, ``settle``/``release`` on the reservation status),
        so replaying is strictly safer than giving up.
        """
        if not self._base_url:
            raise ActionChargeUnavailable("cloud user service URL is empty")
        headers = outbound_headers(self._internal_credential)
        last_error: httpx.HTTPError | None = None
        for _attempt in range(_TRANSPORT_ATTEMPTS):
            try:
                async with httpx.AsyncClient(
                    base_url=self._base_url,
                    timeout=self._timeout_seconds,
                ) as client:
                    return await client.post(
                        path, json=payload, headers=headers, params=params,
                    )
            except httpx.HTTPError as exc:
                last_error = exc
        raise ActionChargeUnavailable(
            "cloud user service unavailable",
        ) from last_error


def _parse_charge(response: httpx.Response) -> ActionCharge:
    try:
        payload = response.json()
    except ValueError as exc:
        raise ActionChargeUnavailable(
            "action charge response is not valid JSON",
        ) from exc
    if not isinstance(payload, dict):
        raise ActionChargeUnavailable(
            "action charge response is not a JSON object",
        )
    no_charge = payload.get("no_charge") is True
    charge_id = str(payload.get("charge_id") or "").strip()
    if not charge_id and not no_charge:
        # A billable charge we cannot name is a leak: nothing could ever
        # settle or release it. Treat it as unavailable so the action runs
        # uncharged instead of stranding a reservation.
        raise ActionChargeUnavailable(
            "action charge response carries no charge id",
        )
    return ActionCharge(
        charge_id=charge_id,
        price_cr=_optional_amount(payload.get("price_cr")),
        no_charge=no_charge,
        gift=_optional_amount(payload.get("gift")),
        purchased=_optional_amount(payload.get("purchased")),
    )


def _optional_amount(raw: object) -> float:
    """Best-effort amount for *display/telemetry only*.

    Unlike the balance read, these numbers never drive a decision — the
    authoritative amount is the ledger row the User service just wrote — so an
    unreadable field degrades to ``0.0`` instead of voiding the charge.
    """
    if isinstance(raw, bool) or raw is None:
        return 0.0
    if isinstance(raw, (int, float)):
        value = float(raw)
    elif isinstance(raw, str):
        try:
            value = float(raw.strip())
        except ValueError:
            return 0.0
    else:
        return 0.0
    return value if isfinite(value) else 0.0


def _price_changed(
    response: httpx.Response,
    *,
    action_key: str,
    expected_price_cr: float | None,
) -> Exception:
    """The 409 quote-binding refusal, tolerating both envelope shapes.

    A 409 that does *not* name ``price_changed`` is some other conflict we have
    no policy for, so it degrades to the usual "unavailable" fail-soft rather
    than being reported to the player as a price move.
    """
    body = response.text
    payload = _json_object(body)
    envelope = payload.get("error")
    if isinstance(envelope, dict):
        code = str(envelope.get("code") or "")
        message = envelope.get("message")
        current = envelope.get("current_price_cr", payload.get("current_price_cr"))
    else:
        code = str(envelope or "")
        message = payload.get("message")
        current = payload.get("current_price_cr")
    if code != PRICE_CHANGED_CODE:
        return ActionChargeUnavailable(
            f"cloud user service returned {response.status_code}",
        )
    return ActionPriceChanged(
        action_key=action_key,
        expected_price_cr=expected_price_cr,
        current_price_cr=_price_or_none(current),
        message=message.strip() if isinstance(message, str) and message.strip() else "",
    )


def _price_or_none(raw: object) -> float | None:
    """The new price, or ``None`` when the answer did not carry a usable one."""
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, (int, float)):
        value = float(raw)
    elif isinstance(raw, str):
        try:
            value = float(raw.strip())
        except ValueError:
            return None
    else:
        return None
    return value if isfinite(value) else None


def _json_object(body: str) -> dict:
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _insufficient_credits(response: httpx.Response) -> ExpectedCloudRefusal:
    body = response.text
    return ExpectedCloudRefusal(
        refusal_summary(response, body),
        request=response.request,
        response=response,
        code=INSUFFICIENT_CREDITS_CODE,
        reason=_refusal_message(body),
    )


def _refusal_message(body: str) -> str:
    """Player-facing reason, tolerating both error-envelope shapes.

    Never falls back to the raw body — that embeds the upstream URL and is one
    of the things the U4 mapping exists to keep away from players.
    """
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return _FALLBACK_REFUSAL_MESSAGE
    if not isinstance(payload, dict):
        return _FALLBACK_REFUSAL_MESSAGE
    envelope = payload.get("error")
    source = envelope if isinstance(envelope, dict) else payload
    message = source.get("message")
    if isinstance(message, str) and message.strip():
        return message.strip()
    return _FALLBACK_REFUSAL_MESSAGE
