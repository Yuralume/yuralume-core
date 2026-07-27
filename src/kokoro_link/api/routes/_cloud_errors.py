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

**Only** ``insufficient_credits`` is remapped. Every other refusal
(``entitlement_denied`` and friends) is a deployment/entitlement fault the
player cannot act on, so it keeps its existing loud behaviour untouched.

Self-host deployments never talk to a cloud gateway, so no code path here can
fire — the guard is a transparent pass-through.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from fastapi import HTTPException, status

from kokoro_link.infrastructure.llm.cloud_refusal import (
    INSUFFICIENT_CREDITS_CODE,
    expected_refusal,
)

_FALLBACK_MESSAGE = "insufficient credits for this request"

__all__ = [
    "INSUFFICIENT_CREDITS_CODE",
    "insufficient_credits_detail",
    "insufficient_credits_guard",
    "insufficient_credits_http_error",
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


@contextmanager
def insufficient_credits_guard() -> Iterator[None]:
    """Re-raise an out-of-credits refusal as HTTP 402; pass everything else on.

    Wrap only the generating call, inside any existing ``try`` — the
    ``HTTPException`` this raises is not one of the domain error types those
    handlers catch, so it travels straight to the client while their mappings
    stay exactly as they were.
    """
    try:
        yield
    except Exception as exc:
        error = insufficient_credits_http_error(exc)
        if error is None:
            raise
        raise error from exc
