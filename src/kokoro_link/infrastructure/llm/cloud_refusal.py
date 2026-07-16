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


class ExpectedCloudRefusal(httpx.HTTPStatusError):
    """A deliberate, non-retryable refusal from the cloud gateway.

    Subclasses :class:`httpx.HTTPStatusError` so existing transport-error
    handling keeps treating it as an HTTP failure; ``code`` carries the
    gateway's machine-readable reason (e.g. ``entitlement_denied``).
    """

    def __init__(
        self,
        message: str,
        *,
        request: httpx.Request,
        response: httpx.Response,
        code: str,
    ) -> None:
        super().__init__(message, request=request, response=response)
        self.code = code


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
    seen = 0
    while exc is not None and seen < _MAX_CAUSE_DEPTH:
        if isinstance(exc, ExpectedCloudRefusal):
            return True
        exc = exc.__cause__
        seen += 1
    return False
