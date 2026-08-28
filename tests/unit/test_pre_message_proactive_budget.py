"""Unit tests for the pure pre-message eligibility decisions.

Covers both mechanical floors under the pre-message window: TR2-A's
budget (how many pushes, how far apart) and TR2-B's delay window (not
before the character is old enough). The dispatcher-level behaviour
(which players they apply to, when they stop applying) lives in
``test_proactive_dispatcher.py``; this file pins the arithmetic and the
reason strings downstream surfaces recognise.
"""

from datetime import datetime, timedelta, timezone

from kokoro_link.application.services.pre_message_proactive_budget import (
    PRE_MESSAGE_CAP_REASON,
    PRE_MESSAGE_DELAY_REASON,
    PRE_MESSAGE_INTERVAL_REASON,
    PRE_MESSAGE_PROACTIVE_CAP,
    PRE_MESSAGE_PROACTIVE_DELAY_HOURS,
    PRE_MESSAGE_PROACTIVE_MIN_INTERVAL_HOURS,
    evaluate_pre_message_proactive_budget,
    evaluate_pre_message_proactive_delay,
)
from kokoro_link.domain.entities.proactive_attempt import ProactiveAttempt
from kokoro_link.domain.value_objects.proactive_outcome import ProactiveOutcome
from kokoro_link.domain.value_objects.proactive_trigger import ProactiveTrigger

_NOW = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)


def _sent(at: datetime) -> ProactiveAttempt:
    return ProactiveAttempt.record(
        character_id="c-1",
        trigger=ProactiveTrigger.TICK,
        outcome=ProactiveOutcome.SENT,
        reason="ok",
        message="hi",
        now=at,
    )


def test_no_prior_push_is_allowed() -> None:
    verdict = evaluate_pre_message_proactive_budget((), now=_NOW)

    assert verdict.passed is True
    assert f"0/{PRE_MESSAGE_PROACTIVE_CAP}" in verdict.reason


def test_one_prior_push_long_enough_ago_is_allowed() -> None:
    long_ago = _NOW - timedelta(
        hours=PRE_MESSAGE_PROACTIVE_MIN_INTERVAL_HOURS + 1,
    )

    verdict = evaluate_pre_message_proactive_budget(
        (_sent(long_ago),), now=_NOW,
    )

    assert verdict.passed is True
    assert f"1/{PRE_MESSAGE_PROACTIVE_CAP}" in verdict.reason


def test_cap_blocks_once_the_ceiling_is_reached() -> None:
    stale = _NOW - timedelta(days=30)
    attempts = tuple(
        _sent(stale - timedelta(days=index))
        for index in range(PRE_MESSAGE_PROACTIVE_CAP)
    )

    verdict = evaluate_pre_message_proactive_budget(attempts, now=_NOW)

    # Age cannot buy back a spent budget: the cap is a total, not a rate.
    assert verdict.passed is False
    assert verdict.reason.startswith(PRE_MESSAGE_CAP_REASON)
    assert (
        f"({PRE_MESSAGE_PROACTIVE_CAP}/{PRE_MESSAGE_PROACTIVE_CAP}"
        in verdict.reason
    )


def test_interval_blocks_a_second_push_too_soon() -> None:
    recent = _NOW - timedelta(
        hours=PRE_MESSAGE_PROACTIVE_MIN_INTERVAL_HOURS - 2,
    )

    verdict = evaluate_pre_message_proactive_budget((_sent(recent),), now=_NOW)

    assert verdict.passed is False
    assert verdict.reason.startswith(PRE_MESSAGE_INTERVAL_REASON)
    assert "2.0h left" in verdict.reason


def test_interval_boundary_is_inclusive() -> None:
    exactly = _NOW - timedelta(hours=PRE_MESSAGE_PROACTIVE_MIN_INTERVAL_HOURS)

    verdict = evaluate_pre_message_proactive_budget((_sent(exactly),), now=_NOW)

    assert verdict.passed is True


def test_spacing_is_measured_from_the_newest_row_not_the_first() -> None:
    """Ordering is not trusted — an oldest-first double would leak a push."""
    rows = (
        _sent(_NOW - timedelta(days=10)),
        _sent(_NOW - timedelta(minutes=5)),
    )

    verdict = evaluate_pre_message_proactive_budget(
        rows, now=_NOW, cap=5,
    )

    assert verdict.passed is False
    assert verdict.reason.startswith(PRE_MESSAGE_INTERVAL_REASON)


def test_naive_timestamps_are_read_as_utc() -> None:
    naive = ProactiveAttempt.record(
        character_id="c-1",
        trigger=ProactiveTrigger.TICK,
        outcome=ProactiveOutcome.SENT,
        reason="ok",
        now=_NOW.replace(tzinfo=None) - timedelta(hours=1),
    )

    verdict = evaluate_pre_message_proactive_budget((naive,), now=_NOW)

    assert verdict.passed is False
    assert verdict.reason.startswith(PRE_MESSAGE_INTERVAL_REASON)


def test_clock_skew_blocks_rather_than_sends() -> None:
    future = _NOW + timedelta(hours=6)

    verdict = evaluate_pre_message_proactive_budget((_sent(future),), now=_NOW)

    assert verdict.passed is False
    assert verdict.reason.startswith(PRE_MESSAGE_INTERVAL_REASON)


# --- TR2-B: the delay window ------------------------------------------


def test_delay_window_blocks_a_freshly_created_character() -> None:
    created = _NOW - timedelta(hours=PRE_MESSAGE_PROACTIVE_DELAY_HOURS - 3)

    verdict = evaluate_pre_message_proactive_delay(created, now=_NOW)

    assert verdict.passed is False
    assert verdict.reason.startswith(PRE_MESSAGE_DELAY_REASON)
    assert "3.0h left" in verdict.reason


def test_delay_window_blocks_the_instant_after_creation() -> None:
    """The case the whole rule exists for: an answer to the create button."""
    verdict = evaluate_pre_message_proactive_delay(_NOW, now=_NOW)

    assert verdict.passed is False


def test_delay_window_boundary_is_inclusive() -> None:
    created = _NOW - timedelta(hours=PRE_MESSAGE_PROACTIVE_DELAY_HOURS)

    verdict = evaluate_pre_message_proactive_delay(created, now=_NOW)

    assert verdict.passed is True


def test_delay_window_lapses_for_an_older_character() -> None:
    verdict = evaluate_pre_message_proactive_delay(
        _NOW - timedelta(days=3), now=_NOW,
    )

    assert verdict.passed is True


def test_delay_window_reads_naive_creation_stamps_as_utc() -> None:
    created = (_NOW - timedelta(hours=1)).replace(tzinfo=None)

    verdict = evaluate_pre_message_proactive_delay(created, now=_NOW)

    assert verdict.passed is False


def test_delay_window_clock_skew_blocks_rather_than_sends() -> None:
    """A creation stamp in the future is not "long ago"."""
    verdict = evaluate_pre_message_proactive_delay(
        _NOW + timedelta(hours=5), now=_NOW,
    )

    assert verdict.passed is False


def test_delay_window_without_a_creation_anchor_does_not_fire() -> None:
    """No anchor means the rule cannot speak, not that it blocks forever.

    A character with no ``created_at`` never round-tripped through a row
    that carries the column, which is the opposite of "just created" —
    same reading the freeze reaper and the idle down-shift give a
    missing anchor. The TR2-A budget still bounds the pushes.
    """
    verdict = evaluate_pre_message_proactive_delay(None, now=_NOW)

    assert verdict.passed is True


def test_delay_window_hours_are_configurable_without_editing_logic() -> None:
    created = _NOW - timedelta(hours=5)

    assert evaluate_pre_message_proactive_delay(
        created, now=_NOW, delay_hours=4.0,
    ).passed is True
    assert evaluate_pre_message_proactive_delay(
        created, now=_NOW, delay_hours=12.0,
    ).passed is False
