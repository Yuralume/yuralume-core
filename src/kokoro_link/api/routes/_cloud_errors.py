"""One player-facing HTTP mapping for cloud-gateway credit refusals.

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

Two refusals are remapped, and only two — both are things the *player* can act
on: ``insufficient_credits`` (top up) and ``price_changed`` (send again at the
new price, C1's quote binding). Every other refusal (``entitlement_denied`` and
friends) is a deployment/entitlement fault the player cannot act on, so it
keeps its existing loud behaviour untouched.

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

__all__ = [
    "INSUFFICIENT_CREDITS_CODE",
    "PRICE_CHANGED_CODE",
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
    """Re-raise a player-actionable billing refusal as HTTP; pass the rest on.

    ``402`` for out-of-credits, ``409`` for a moved price. Wrap only the
    generating call, inside any existing ``try`` — the ``HTTPException`` this
    raises is not one of the domain error types those handlers catch, so it
    travels straight to the client while their mappings stay exactly as they
    were.
    """
    try:
        yield
    except Exception as exc:
        error = insufficient_credits_http_error(exc) or _price_changed_http_error(exc)
        if error is None:
            raise
        raise error from exc
