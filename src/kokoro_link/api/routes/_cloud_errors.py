"""Player-facing HTTP mappings for cloud-gateway refusals.

The hosted gateway answers an out-of-credits call with a deliberate
``402 {"error": {"code": "insufficient_credits", "retryable": false}}``. Core
turns that into an :class:`~kokoro_link.infrastructure.llm.cloud_refusal.ExpectedCloudRefusal`,
which — before this module existed — no foreground route caught, so the player
got an opaque **500** at the exact moment they were most willing to top up.

Every player-triggered foreground entry point funnels through the single guard
here rather than repeating the mapping per route:

    with insufficient_credits_guard():
        result = await container.some_service.do_the_thing(...)

The guard inspects the whole ``raise ... from`` chain, so it fires just as well
when the refusal has been re-wrapped into a medium-specific error
(``ImageGenerationError`` → ``GenerationFailedError``, ``TTSError``, …).

Four refusal codes are remapped here, in two families:

- Player-actionable: ``insufficient_credits`` (top up, 402) and
  ``price_changed`` (send again at the new price, C1's quote binding, 409).
- Deployment safety fuse: ``cost_cap_exceeded`` and ``quota_exceeded`` (429).
  These fire when the account has hit its own monthly cost cap or quota — a
  deliberate circuit breaker, not a per-request entitlement decision. The
  player didn't cause it and can't top up their way out of it, but it *is*
  transient (it clears next billing cycle or when the operator raises the
  cap), so it deserves a legible, retryable-shaped 429 rather than collapsing
  into the same opaque 500 as a genuine deployment fault. The gateway's own
  ``retryable: false`` just means "don't retry blindly right now" — it says
  nothing about the player-facing status code.

Every other refusal (``entitlement_denied`` and friends) is a deployment/
entitlement fault the player cannot act on and isn't self-healing on any
predictable timeline, so it keeps its existing loud behaviour untouched.

Self-host deployments never talk to a cloud gateway, so no code path here can
fire — the guard is a transparent pass-through.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from fastapi import HTTPException, status

from kokoro_link.contracts.cloud_action_billing import (
    PRICE_CHANGED_CODE,
    PRICE_CHANGED_MESSAGE,
    ActionPriceChanged,
)
from kokoro_link.infrastructure.llm.cloud_refusal import (
    INSUFFICIENT_CREDITS_CODE,
    expected_refusal,
)

_FALLBACK_MESSAGE = "insufficient credits for this request"
_MAX_CAUSE_DEPTH = 10

#: The gateway's codes for its own deployment-level safety fuse: the account
#: hit its monthly cost cap or usage quota. Distinct from
#: ``insufficient_credits`` (the *player's* balance) — this is the operator's
#: circuit breaker tripping.
COST_CAP_EXCEEDED_CODE = "cost_cap_exceeded"
QUOTA_EXCEEDED_CODE = "quota_exceeded"
_FUSE_TRIPPED_CODES = frozenset({COST_CAP_EXCEEDED_CODE, QUOTA_EXCEEDED_CODE})
_FUSE_TRIPPED_FALLBACK_MESSAGE = (
    "this deployment has hit its cost or usage safety limit; please try again later"
)

__all__ = [
    "COST_CAP_EXCEEDED_CODE",
    "INSUFFICIENT_CREDITS_CODE",
    "PRICE_CHANGED_CODE",
    "QUOTA_EXCEEDED_CODE",
    "fuse_tripped_detail",
    "insufficient_credits_detail",
    "insufficient_credits_guard",
    "insufficient_credits_http_error",
    "price_changed_detail",
]


def insufficient_credits_detail(exc: BaseException | None) -> dict[str, str] | None:
    """Structured error body when ``exc`` means "out of credits", else ``None``.

    Shaped like the existing ``subscription_frozen`` 403 detail so the frontend
    reads every entitlement outcome the same way. Deliberately carries the
    *gateway's* short reason rather than ``str(exc)``, which embeds the upstream
    URL and body preview.
    """
    refusal = expected_refusal(exc)
    if refusal is None or refusal.code != INSUFFICIENT_CREDITS_CODE:
        return None
    return {
        "code": INSUFFICIENT_CREDITS_CODE,
        "message": refusal.reason or _FALLBACK_MESSAGE,
    }


def insufficient_credits_http_error(
    exc: BaseException | None,
) -> HTTPException | None:
    """``402 Payment Required`` for an out-of-credits refusal, else ``None``.

    402 is the accurate shape: the request is well-formed and authorized, it
    simply cannot be paid for — retrying succeeds once the player tops up.
    """
    detail = insufficient_credits_detail(exc)
    if detail is None:
        return None
    return HTTPException(
        status_code=status.HTTP_402_PAYMENT_REQUIRED,
        detail=detail,
    )


def price_changed_detail(exc: BaseException | None) -> dict[str, object] | None:
    """Structured error body when the quoted price moved, else ``None``.

    Carries ``current_price_cr`` so the SPA can show the new number alongside
    its localized "the price was updated, please send again" copy instead of
    making the player go and look it up.
    """
    changed = _find_price_changed(exc)
    if changed is None:
        return None
    detail: dict[str, object] = {
        "code": PRICE_CHANGED_CODE,
        "message": changed.reason or PRICE_CHANGED_MESSAGE,
    }
    if changed.current_price_cr is not None:
        detail["current_price_cr"] = changed.current_price_cr
    return detail


def fuse_tripped_detail(exc: BaseException | None) -> dict[str, str] | None:
    """Structured error body when ``exc`` means "cost cap / quota fuse tripped".

    Shaped identically to :func:`insufficient_credits_detail` so the frontend
    reads every guard-mapped refusal the same way — only the ``code`` differs
    between ``cost_cap_exceeded`` and ``quota_exceeded``.
    """
    refusal = expected_refusal(exc)
    if refusal is None or refusal.code not in _FUSE_TRIPPED_CODES:
        return None
    return {
        "code": refusal.code,
        "message": refusal.reason or _FUSE_TRIPPED_FALLBACK_MESSAGE,
    }


def _fuse_tripped_http_error(exc: BaseException | None) -> HTTPException | None:
    """``429 Too Many Requests`` for a tripped cost-cap/quota fuse, else ``None``.

    429 rather than 500: the request itself was fine, but the deployment's own
    safety fuse — not the player's balance — is what refused it. Distinct from
    the player-actionable 402/409 above: nothing the player does resolves
    this, but it is a legible, expected-to-clear-eventually condition rather
    than a fault worth an opaque stack trace.
    """
    detail = fuse_tripped_detail(exc)
    if detail is None:
        return None
    return HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=detail)


def _price_changed_http_error(exc: BaseException | None) -> HTTPException | None:
    """``409 Conflict``: the request is valid, the quote it carried is not."""
    detail = price_changed_detail(exc)
    if detail is None:
        return None
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


def _find_price_changed(exc: BaseException | None) -> ActionPriceChanged | None:
    """The price-change refusal ``exc`` is, or wraps via ``raise ... from``."""
    seen = 0
    while exc is not None and seen < _MAX_CAUSE_DEPTH:
        if isinstance(exc, ActionPriceChanged):
            return exc
        exc = exc.__cause__
        seen += 1
    return None


@contextmanager
def insufficient_credits_guard() -> Iterator[None]:
    """Re-raise a legible-to-the-player billing refusal as HTTP; pass the rest on.

    ``402`` for out-of-credits, ``409`` for a moved price, ``429`` for a
    tripped cost-cap/quota fuse. Wrap only the generating call, inside any
    existing ``try`` — the ``HTTPException`` this raises is not one of the
    domain error types those handlers catch, so it travels straight to the
    client while their mappings stay exactly as they were.
    """
    try:
        yield
    except Exception as exc:
        error = (
            insufficient_credits_http_error(exc)
            or _price_changed_http_error(exc)
            or _fuse_tripped_http_error(exc)
        )
        if error is None:
            raise
        raise error from exc
