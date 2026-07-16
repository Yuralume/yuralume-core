"""Diagnostics for upstream HTTP provider failures."""

from __future__ import annotations

import logging

import httpx

_BODY_PREVIEW_CHARS = 2000
_REQUEST_ID_HEADERS = (
    "x-request-id",
    "x-openai-request-id",
    "request-id",
    "x-correlation-id",
    "cf-ray",
)


def log_http_error_response(
    logger: logging.Logger,
    response: httpx.Response,
    *,
    operation: str,
    body_text: str | None = None,
    max_body_chars: int = _BODY_PREVIEW_CHARS,
) -> None:
    """Log a non-success provider response before raising.

    The request body and headers are intentionally omitted because provider
    calls often contain secrets, full prompts, or user-supplied media refs.
    """
    if response.status_code < 400:
        return
    body = _truncate(
        body_text if body_text is not None else _safe_response_text(response),
        max_body_chars,
    )
    method, url = _request_summary(response)
    upstream_request_id = _upstream_request_id(response)
    logger.error(
        "%s HTTP %s from %s %s (upstream_request_id=%s): %s",
        operation,
        response.status_code,
        method,
        url,
        upstream_request_id or "-",
        body,
    )


def log_expected_refusal(
    logger: logging.Logger,
    response: httpx.Response,
    *,
    operation: str,
    code: str,
    message: str,
    context: str = "",
    max_message_chars: int = 240,
) -> None:
    """Log a deliberate, non-retryable gateway refusal at WARNING.

    Unlike :func:`log_http_error_response`, this stays off the ERROR channel
    and drops the bulky body preview: a refusal is an expected control-plane
    outcome, so a single concise, correlatable line is all that's warranted.
    """
    method, url = _request_summary(response)
    upstream_request_id = _upstream_request_id(response)
    logger.warning(
        "%s refused (code=%s) %s %s%s (upstream_request_id=%s): %s",
        operation,
        code,
        method,
        url,
        f" [{context}]" if context else "",
        upstream_request_id or "-",
        _truncate(message, max_message_chars),
    )


def _safe_response_text(response: httpx.Response) -> str:
    try:
        return response.text
    except Exception as exc:  # pragma: no cover - defensive for stream misuse.
        return f"<response body unavailable: {exc}>"


def _request_summary(response: httpx.Response) -> tuple[str, str]:
    try:
        request = response.request
    except RuntimeError:
        return "UNKNOWN", "<unknown-url>"
    return request.method, str(request.url)


def _upstream_request_id(response: httpx.Response) -> str:
    for header in _REQUEST_ID_HEADERS:
        value = response.headers.get(header)
        if value:
            return value
    return ""


def _truncate(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return text[:max_chars] + "...<truncated>"
