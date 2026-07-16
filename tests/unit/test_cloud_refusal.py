from __future__ import annotations

import json
import logging

import httpx
import pytest

from kokoro_link.infrastructure.llm.cloud_refusal import (
    ExpectedCloudRefusal,
    classify_refusal,
    log_auxiliary_llm_failure,
)


def _entitlement_body() -> str:
    return json.dumps({
        "error": {
            "code": "entitlement_denied",
            "message": "forwarded account is inactive or outside the forwarded tenant",
            "retryable": False,
        },
    })


def test_classify_refusal_flags_non_retryable_4xx() -> None:
    result = classify_refusal(403, _entitlement_body())
    assert result == (
        "entitlement_denied",
        "forwarded account is inactive or outside the forwarded tenant",
    )


def test_classify_refusal_ignores_retryable_true() -> None:
    body = json.dumps({"error": {"code": "busy", "retryable": True}})
    assert classify_refusal(429, body) is None


def test_classify_refusal_ignores_5xx_even_when_non_retryable() -> None:
    # A 5xx is a server fault, not a deliberate policy refusal — stay loud.
    assert classify_refusal(503, _entitlement_body()) is None


def test_classify_refusal_ignores_unstructured_body() -> None:
    assert classify_refusal(400, "invalid model preset") is None
    assert classify_refusal(400, json.dumps({"error": "flat string"})) is None


def _refusal() -> ExpectedCloudRefusal:
    request = httpx.Request("POST", "https://gateway.example/v1/chat/completions")
    response = httpx.Response(403, request=request, text="denied")
    return ExpectedCloudRefusal(
        "denied", request=request, response=response, code="entitlement_denied",
    )


def test_log_auxiliary_llm_failure_skips_expected_refusal(
    caplog: pytest.LogCaptureFixture,
) -> None:
    logger = logging.getLogger("test.aux.refusal")
    with caplog.at_level(logging.DEBUG, logger=logger.name):
        log_auxiliary_llm_failure(logger, _refusal(), "feature call failed")
    assert caplog.records == []


def test_log_auxiliary_llm_failure_skips_wrapped_refusal(
    caplog: pytest.LogCaptureFixture,
) -> None:
    logger = logging.getLogger("test.aux.wrapped")
    try:
        raise RuntimeError("scene judge failed") from _refusal()
    except RuntimeError as exc:
        with caplog.at_level(logging.DEBUG, logger=logger.name):
            log_auxiliary_llm_failure(logger, exc, "judge failed")
    assert caplog.records == []


def test_log_auxiliary_llm_failure_errors_on_genuine_fault(
    caplog: pytest.LogCaptureFixture,
) -> None:
    logger = logging.getLogger("test.aux.fault")
    with caplog.at_level(logging.ERROR, logger=logger.name):
        log_auxiliary_llm_failure(
            logger, ValueError("boom"), "feed composer failed character=%s", "c1",
        )
    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.levelno == logging.ERROR
    assert record.message == "feed composer failed character=c1"
    assert record.exc_info is not None
