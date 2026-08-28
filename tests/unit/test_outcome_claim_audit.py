"""HV3 — the per-round audit trail (``outcome_claim_audit``).

Pure unit tests of the ``ContextVar`` accumulator itself: scoping,
isolation across concurrent scopes, and the summary reducer. The guard's
own emission of these events is pinned separately in
``test_outcome_claim_judge.py`` (unit) and
``test_pending_follow_up_dispatcher_honesty_audit.py`` (the caller that
folds the summary into a turn record).
"""

from __future__ import annotations

import asyncio

from kokoro_link.application.services.outcome_claim_audit import (
    current_outcome_claim_audit,
    outcome_claim_audit_scope,
    outcome_claim_audit_summary,
    record_block_event,
    record_corrected_event,
    record_judge_event,
    record_parked_event,
)
from kokoro_link.contracts.outcome_claim import (
    OUTCOME_CLAIM_CONSISTENT,
    OUTCOME_CLAIM_INCONSISTENT,
    OUTCOME_CLAIM_UNAVAILABLE,
)


def test_outside_a_scope_events_are_dropped_not_queued() -> None:
    """No caller opted in — must be a true no-op, not silent accumulation
    that leaks into whichever scope opens next."""
    record_judge_event(status=OUTCOME_CLAIM_CONSISTENT)
    record_block_event(after_tools=False)
    assert current_outcome_claim_audit() == ()
    assert outcome_claim_audit_summary() is None


def test_summary_is_none_when_the_scope_never_reached_the_judge() -> None:
    """No tools offered / guard unwired: the scope opens and closes with
    nothing in it. Distinguishing this from "reviewed but consistent" is
    the whole point of returning ``None`` rather than an empty dict."""
    with outcome_claim_audit_scope():
        summary = outcome_claim_audit_summary()
    assert summary is None


def test_a_single_consistent_verdict_summarises_cleanly() -> None:
    with outcome_claim_audit_scope():
        record_judge_event(status=OUTCOME_CLAIM_CONSISTENT)
        summary = outcome_claim_audit_summary()

    assert summary == {
        "verdicts": [OUTCOME_CLAIM_CONSISTENT],
        "final_verdict": OUTCOME_CLAIM_CONSISTENT,
        "unsupported_claim_counts": [0],
        "any_truncated": False,
        "blocked_count": 0,
        "blocked_after_tools": False,
        "corrected_count": 0,
        "parked": False,
        "parked_reason": "",
        "parked_kind": "",
    }


def test_a_blocked_then_corrected_round() -> None:
    """The zero-call exit's happy-path retry: one bad verdict, one block,
    one correction — no second judge call needed when the retry answered
    with a real tool call instead of prose."""
    with outcome_claim_audit_scope():
        record_judge_event(status=OUTCOME_CLAIM_INCONSISTENT, unsupported_claim_count=2)
        record_block_event(after_tools=False)
        record_corrected_event()
        summary = outcome_claim_audit_summary()

    assert summary["verdicts"] == [OUTCOME_CLAIM_INCONSISTENT]
    assert summary["final_verdict"] == OUTCOME_CLAIM_INCONSISTENT
    assert summary["unsupported_claim_counts"] == [2]
    assert summary["blocked_count"] == 1
    assert summary["blocked_after_tools"] is False
    assert summary["corrected_count"] == 1
    assert summary["parked"] is False


def test_a_two_phase_round_both_judge_calls_are_kept() -> None:
    """Zero-call block → real tool call → after-tools verdict. Two
    ``judge`` events, in order, is the whole reason this is an event
    list rather than one flat verdict field."""
    with outcome_claim_audit_scope():
        record_judge_event(status=OUTCOME_CLAIM_INCONSISTENT, unsupported_claim_count=1)
        record_block_event(after_tools=False)
        record_corrected_event()
        record_judge_event(status=OUTCOME_CLAIM_CONSISTENT)
        summary = outcome_claim_audit_summary()

    assert summary["verdicts"] == [
        OUTCOME_CLAIM_INCONSISTENT, OUTCOME_CLAIM_CONSISTENT,
    ]
    assert summary["final_verdict"] == OUTCOME_CLAIM_CONSISTENT
    assert summary["blocked_count"] == 1
    assert summary["corrected_count"] == 1
    assert summary["parked"] is False


def test_a_park_after_an_unavailable_verdict() -> None:
    with outcome_claim_audit_scope():
        record_judge_event(status=OUTCOME_CLAIM_UNAVAILABLE)
        record_parked_event(reason="no verdict at the zero-call exit")
        summary = outcome_claim_audit_summary()

    assert summary["verdicts"] == [OUTCOME_CLAIM_UNAVAILABLE]
    assert summary["blocked_count"] == 0
    assert summary["parked"] is True
    assert summary["parked_reason"] == "no verdict at the zero-call exit"


def test_a_structural_park_after_a_block_keeps_both_facts() -> None:
    """Blocked once, then parked because the payload could not carry a
    correction — both facts belong on the same round's row."""
    with outcome_claim_audit_scope():
        record_judge_event(status=OUTCOME_CLAIM_INCONSISTENT, unsupported_claim_count=1)
        record_block_event(after_tools=True)
        record_parked_event(reason="payload cannot carry a correction")
        summary = outcome_claim_audit_summary()

    assert summary["blocked_count"] == 1
    assert summary["blocked_after_tools"] is True
    assert summary["corrected_count"] == 0
    assert summary["parked"] is True
    assert summary["parked_reason"] == "payload cannot carry a correction"


def test_a_truncated_verdict_is_flagged_in_the_summary() -> None:
    """S5: a ``consistent`` verdict reached over only a prefix must still
    read as ``consistent`` (this is not a block) — but the summary must
    say the round had an unseen tail, which is the trace that answer
    must never lack."""
    with outcome_claim_audit_scope():
        record_judge_event(status=OUTCOME_CLAIM_CONSISTENT, truncated=True)
        summary = outcome_claim_audit_summary()

    assert summary["final_verdict"] == OUTCOME_CLAIM_CONSISTENT
    assert summary["any_truncated"] is True


def test_a_later_full_verdict_does_not_erase_an_earlier_truncation() -> None:
    """Zero-call offence on a truncated message, corrected by a real tool
    call whose after-tools verdict reviews the full body: the round still
    had an unseen tail at some point, and the summary must not lose that
    fact just because the *final* verdict happened to see everything."""
    with outcome_claim_audit_scope():
        record_judge_event(
            status=OUTCOME_CLAIM_INCONSISTENT,
            unsupported_claim_count=1, truncated=True,
        )
        record_block_event(after_tools=False)
        record_corrected_event()
        record_judge_event(status=OUTCOME_CLAIM_CONSISTENT, truncated=False)
        summary = outcome_claim_audit_summary()

    assert summary["final_verdict"] == OUTCOME_CLAIM_CONSISTENT
    assert summary["any_truncated"] is True


def test_events_never_carry_message_text() -> None:
    """The log de-identification redline, pinned as a shape assertion:
    no field on the event or the summary can hold arbitrary prose —
    only enum status strings, counts, and short static reason phrases
    the caller itself controls."""
    with outcome_claim_audit_scope():
        record_judge_event(status=OUTCOME_CLAIM_INCONSISTENT, unsupported_claim_count=3)
        record_parked_event(reason="correction pass wrote nothing")
        events = current_outcome_claim_audit()

    for event in events:
        assert not hasattr(event, "message_text")
        assert not hasattr(event, "unsupported_claims")


async def _run_scoped_round(marker: str, *, delay: float) -> tuple[str, object]:
    with outcome_claim_audit_scope():
        record_judge_event(status=marker)
        await asyncio.sleep(delay)
        # A second event appended after yielding control proves the scope
        # survived the ``await`` — the whole point of a ContextVar over a
        # plain module-level list.
        record_block_event(after_tools=False)
        return marker, outcome_claim_audit_summary()


async def _concurrent_scopes_do_not_see_each_other() -> None:
    (marker_a, summary_a), (marker_b, summary_b) = await asyncio.gather(
        _run_scoped_round(OUTCOME_CLAIM_INCONSISTENT, delay=0.02),
        _run_scoped_round(OUTCOME_CLAIM_CONSISTENT, delay=0.0),
    )
    assert marker_a == OUTCOME_CLAIM_INCONSISTENT
    assert marker_b == OUTCOME_CLAIM_CONSISTENT
    assert summary_a["verdicts"] == [OUTCOME_CLAIM_INCONSISTENT]
    assert summary_a["blocked_count"] == 1
    assert summary_b["verdicts"] == [OUTCOME_CLAIM_CONSISTENT]
    assert summary_b["blocked_count"] == 1
    # Neither round's event leaked into the other's trail.
    assert summary_a["verdicts"] != summary_b["verdicts"]


def test_concurrent_asyncio_tasks_keep_isolated_scopes() -> None:
    """Two rounds — different characters, interleaved awaits — must never
    see each other's events. This is the property the whole ContextVar
    design exists for; a module-level list would fail this test."""
    asyncio.run(_concurrent_scopes_do_not_see_each_other())
