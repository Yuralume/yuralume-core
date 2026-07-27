"""Cloud control-plane refusals are a first-class outcome, not a fault.

When the hosted gateway answers a generation call with a deliberate,
non-retryable policy decision (entitlement revoked, quota exhausted), that is
the cloud doing its job — the deployment must treat it as an expected, quiet
skip rather than an error worth a stack trace.

Classifying it here keeps core free of any tenant / tier / demo-specific
knowledge: it reacts only to the cloud's own ``retryable: false`` signal,
leaving *who* is entitled (and why) entirely to the cloud control plane.
"""

from __future__ import annotations

import json
import logging

import httpx

_MAX_CAUSE_DEPTH = 10
_MAX_SUMMARY_BODY_CHARS = 500

#: The gateway's code for "this tenant has no credits left". The one refusal
#: the player can act on themselves (top up), so it gets a dedicated
#: player-facing HTTP mapping instead of the generic loud-error path.
INSUFFICIENT_CREDITS_CODE = "insufficient_credits"


class ExpectedCloudRefusal(httpx.HTTPStatusError):
    """A deliberate, non-retryable refusal from the cloud gateway.

    Subclasses :class:`httpx.HTTPStatusError` so existing transport-error
    handling keeps treating it as an HTTP failure; ``code`` carries the
    gateway's machine-readable reason (e.g. ``entitlement_denied``) and
    ``reason`` its human-readable one, kept separate from the diagnostic
    ``str(exc)`` summary (which embeds the upstream URL and body preview and
    must never reach a player).
    """

    def __init__(
        self,
        message: str,
        *,
        request: httpx.Request,
        response: httpx.Response,
        code: str,
        reason: str = "",
    ) -> None:
        super().__init__(message, request=request, response=response)
        self.code = code
        self.reason = reason


def classify_refusal(status_code: int, body_text: str) -> tuple[str, str] | None:
    """Return ``(code, message)`` when the gateway deliberately refused.

    A refusal is a client-side (4xx) response whose JSON error envelope is
    flagged ``retryable: false`` — the cloud stating the call will never
    succeed as-is (policy / entitlement / quota), as opposed to a transient
    fault. Anything else returns ``None`` and stays on the loud error path.
    """
    if not 400 <= status_code < 500:
        return None
    try:
        payload = json.loads(body_text)
    except (json.JSONDecodeError, TypeError):
        return None
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict) or error.get("retryable") is not False:
        return None
    code = str(error.get("code") or "refused")
    message = str(error.get("message") or "")
    return code, message


def refusal_from_response(
    response: httpx.Response,
    body_text: str,
    *,
    summary: str = "",
) -> ExpectedCloudRefusal | None:
    """Build the typed refusal for ``response``, or ``None`` if it isn't one.

    Every cloud gateway adapter (LLM, image, TTS, video) funnels through this
    one factory so a refusal is classified identically no matter which medium
    hit it — media adapters then re-raise their own error type ``from`` this
    one, keeping the code reachable via :func:`expected_refusal_code`.
    """
    classified = classify_refusal(response.status_code, body_text)
    if classified is None:
        return None
    code, message = classified
    return ExpectedCloudRefusal(
        summary or refusal_summary(response, body_text),
        request=response.request,
        response=response,
        code=code,
        reason=message,
    )


def refusal_summary(response: httpx.Response, body_text: str) -> str:
    """Diagnostic one-liner for logs / ``str(exc)`` — never player-facing."""
    return (
        f"{response.status_code} from {response.request.url}: "
        f"{body_text[:_MAX_SUMMARY_BODY_CHARS]}"
    )


def expected_refusal_code(exc: BaseException | None) -> str | None:
    """The gateway refusal code behind ``exc``, following ``raise ... from``.

    Foreground callers re-wrap upstream errors into their own types (image →
    ``ImageGenerationError`` → ``GenerationFailedError``), so the refusal is
    usually a few links down the ``__cause__`` chain rather than the exception
    itself.
    """
    refusal = _find_expected_refusal(exc)
    return None if refusal is None else refusal.code


def expected_refusal(exc: BaseException | None) -> ExpectedCloudRefusal | None:
    """The refusal behind ``exc`` (see :func:`expected_refusal_code`)."""
    return _find_expected_refusal(exc)


def log_auxiliary_llm_failure(
    logger: logging.Logger,
    exc: BaseException,
    message: str,
    *args: object,
) -> None:
    """Log a background auxiliary-LLM failure at the right severity.

    Expected cloud refusals are skipped silently here — the gateway already
    emitted one concise WARNING with full identity context, so re-logging at
    every auxiliary processor would only add noise. Genuine faults keep the
    ERROR + traceback they had before.
    """
    if _is_expected_refusal(exc):
        return
    logger.error(message, *args, exc_info=exc)


def _is_expected_refusal(exc: BaseException | None) -> bool:
    """True if ``exc`` is — or wraps (via ``raise ... from``) — a refusal."""
    return _find_expected_refusal(exc) is not None


def _find_expected_refusal(
    exc: BaseException | None,
) -> ExpectedCloudRefusal | None:
    """The refusal ``exc`` is, or wraps via ``raise ... from``, if any."""
    seen = 0
    while exc is not None and seen < _MAX_CAUSE_DEPTH:
        if isinstance(exc, ExpectedCloudRefusal):
            return exc
        exc = exc.__cause__
        seen += 1
    return None
