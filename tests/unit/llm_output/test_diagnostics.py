"""The point of the diagnostics module: failures stop being invisible.

Every migrated site is fail-soft, and stays fail-soft. What changes is
that an unreadable reply now leaves a record with a site label on it,
so "this deployment truncates post-turn output on every long turn" is
distinguishable from "this one gets prose where it asked for JSON"
without attaching a debugger to production.

Two constraints are as important as the logging itself: the raw text
never reaches the log (model output is player content), and the helper
can never raise (it runs inside handlers that must not die).
"""

from __future__ import annotations

import logging

import pytest

from kokoro_link.llm_output import (
    ParseOutcome,
    ParseReason,
    extract_object_outcome,
    log_parse_outcome,
)


_LOGGER = logging.getLogger("tests.llm_output.diagnostics")


def test_a_failure_warns_with_its_site_and_reason(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger=_LOGGER.name):
        log_parse_outcome(
            _LOGGER, extract_object_outcome("完全是散文"), site="post_turn.llm_processor",
        )

    (record,) = caplog.records
    assert record.levelno == logging.WARNING
    assert "post_turn.llm_processor" in record.getMessage()
    assert "no_json" in record.getMessage()


def test_a_repair_informs_rather_than_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A repaired payload is a success with a caveat: the value is real,
    but something at the tail is missing because the upstream cut the
    reply short."""
    with caplog.at_level(logging.INFO, logger=_LOGGER.name):
        log_parse_outcome(
            _LOGGER, extract_object_outcome('{"a": 1'), site="chat.tool_call_parser",
        )

    (record,) = caplog.records
    assert record.levelno == logging.INFO
    assert "chat.tool_call_parser" in record.getMessage()


def test_a_clean_parse_says_nothing(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG, logger=_LOGGER.name):
        log_parse_outcome(
            _LOGGER, extract_object_outcome('{"a": 1}'), site="anywhere",
        )
    assert caplog.records == []


def test_the_raw_reply_never_reaches_the_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "森森說他住在東京都新宿區"
    raw = f'{{"memories": [{{"content": "{secret}"'
    with caplog.at_level(logging.DEBUG, logger=_LOGGER.name):
        log_parse_outcome(
            _LOGGER, extract_object_outcome(raw, repair_truncated=False), site="memory",
        )

    (record,) = caplog.records
    assert secret not in record.getMessage()
    assert str(len(raw)) in record.getMessage()


def test_logging_failures_never_propagate() -> None:
    """The helper runs on a path whose whole job is not to break a turn.
    A misconfigured handler must not become the thing that does."""

    class _BrokenLogger:
        def info(self, *args: object, **kwargs: object) -> None:
            raise RuntimeError("handler exploded")

        def warning(self, *args: object, **kwargs: object) -> None:
            raise RuntimeError("handler exploded")

    log_parse_outcome(
        _BrokenLogger(),  # type: ignore[arg-type]
        ParseOutcome(None, ParseReason.UNBALANCED, 12),
        site="anywhere",
    )


@pytest.mark.parametrize(
    ("reason", "expected_level"),
    [
        (ParseReason.NO_JSON, logging.WARNING),
        (ParseReason.UNBALANCED, logging.WARNING),
        (ParseReason.DECODE_ERROR, logging.WARNING),
    ],
)
def test_every_failure_reason_is_visible(
    reason: ParseReason,
    expected_level: int,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No reason code may be silently dropped — a new one added without
    a level would otherwise re-create the invisibility this exists to
    remove."""
    with caplog.at_level(logging.DEBUG, logger=_LOGGER.name):
        log_parse_outcome(_LOGGER, ParseOutcome(None, reason, 5), site="s")

    (record,) = caplog.records
    assert record.levelno == expected_level
    assert reason.value in record.getMessage()
