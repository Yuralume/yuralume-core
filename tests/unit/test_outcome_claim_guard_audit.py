"""HV3 — ``OutcomeClaimGuard`` feeds the per-round audit trail.

Where ``test_outcome_claim_audit.py`` tests the ``ContextVar`` accumulator
in isolation and ``test_composer_tool_loop_honesty.py`` tests the loop's
reaction to a verdict, this file is the seam between them: does calling
the guard's existing methods — unmodified signatures, same call shape the
loop has always used — actually populate an open audit scope with the
right events, and stay silent when nobody opened one.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from kokoro_link.application.services.outcome_claim_audit import (
    outcome_claim_audit_scope,
    outcome_claim_audit_summary,
)
from kokoro_link.application.services.outcome_claim_guard import (
    OutcomeClaimGuard,
)
from kokoro_link.contracts.outcome_claim import (
    OUTCOME_CLAIM_CONSISTENT,
    OUTCOME_CLAIM_INCONSISTENT,
    OUTCOME_CLAIM_UNAVAILABLE,
    OutcomeClaimEvidence,
    OutcomeClaimVerdict,
)


@dataclass
class _ScriptedJudge:
    verdicts: list[OutcomeClaimVerdict]
    seen: list[str] = field(default_factory=list)

    async def judge(self, *, message_text, evidence, character=None,
                     operator_primary_language="") -> OutcomeClaimVerdict:
        self.seen.append(message_text)
        return self.verdicts.pop(0)


class _RaisingJudge:
    async def judge(self, **_kwargs) -> OutcomeClaimVerdict:
        raise RuntimeError("upstream down")


def _evidence() -> OutcomeClaimEvidence:
    return OutcomeClaimEvidence(offered_tools=("fake_image",))


@pytest.mark.asyncio
async def test_a_consistent_review_emits_one_judge_event() -> None:
    guard = OutcomeClaimGuard(judge=_ScriptedJudge([OutcomeClaimVerdict.ok()]))

    with outcome_claim_audit_scope():
        await guard.review(message_text="晚點畫給你", evidence=_evidence())
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


@pytest.mark.asyncio
async def test_review_outside_a_scope_touches_nothing() -> None:
    """The overwhelmingly common caller (no audit scope opened) must pay
    no cost and leave no residue for the next scope that does open."""
    guard = OutcomeClaimGuard(judge=_ScriptedJudge([OutcomeClaimVerdict.ok()]))

    await guard.review(message_text="晚點畫給你", evidence=_evidence())

    with outcome_claim_audit_scope():
        assert outcome_claim_audit_summary() is None


@pytest.mark.asyncio
async def test_blocked_then_corrected_records_both_events() -> None:
    guard = OutcomeClaimGuard(judge=_ScriptedJudge([
        OutcomeClaimVerdict.blocked(("圖片附上",)),
    ]))

    with outcome_claim_audit_scope():
        verdict = await guard.review(
            message_text="畫好囉！圖片附上～", evidence=_evidence(),
        )
        assert verdict.inconsistent
        guard.record_block(after_tools=False)
        guard.record_corrected()
        summary = outcome_claim_audit_summary()

    assert summary["verdicts"] == [OUTCOME_CLAIM_INCONSISTENT]
    assert summary["unsupported_claim_counts"] == [1]
    assert summary["blocked_count"] == 1
    assert summary["blocked_after_tools"] is False
    assert summary["corrected_count"] == 1
    assert summary["parked"] is False


@pytest.mark.asyncio
async def test_judge_failure_records_unavailable_and_a_park_reason() -> None:
    guard = OutcomeClaimGuard(judge=_RaisingJudge())

    with outcome_claim_audit_scope():
        verdict = await guard.review(
            message_text="圖片已經傳過去囉", evidence=_evidence(),
        )
        assert verdict.unavailable
        guard.record_parked(reason="no verdict at the zero-call exit")
        summary = outcome_claim_audit_summary()

    assert summary["verdicts"] == [OUTCOME_CLAIM_UNAVAILABLE]
    assert summary["parked"] is True
    assert summary["parked_reason"] == "no verdict at the zero-call exit"
    # The judge_failed counter still moves — the audit trail is additive,
    # not a replacement for the aggregate counters.
    assert guard.counters.judge_failed == 1


@pytest.mark.asyncio
async def test_record_parked_default_reason_is_backward_compatible() -> None:
    """Every pre-HV3 call site calls ``record_parked()`` with no argument
    (``composer_tool_loop`` is not in this ticket's file group). The
    signature must keep accepting that shape."""
    guard = OutcomeClaimGuard(judge=_RaisingJudge())

    with outcome_claim_audit_scope():
        await guard.review(message_text="查好了", evidence=_evidence())
        guard.record_parked()
        summary = outcome_claim_audit_summary()

    assert summary["parked"] is True
    assert summary["parked_reason"] == ""


@pytest.mark.asyncio
async def test_empty_text_short_circuit_records_no_judge_event() -> None:
    """No model call happened — nothing to audit either."""
    guard = OutcomeClaimGuard(judge=_ScriptedJudge([]))

    with outcome_claim_audit_scope():
        verdict = await guard.review(message_text="   ", evidence=_evidence())
        summary = outcome_claim_audit_summary()

    assert verdict.consistent
    assert summary is None
