"""Prometheus rendering for the HV1 outcome-claim honesty gate.

The gate's whole failure mode is silence. It fails **closed** — a judge
that cannot answer parks the promise rather than letting it ship — which
means an unreachable judge produces no error a player or an operator ever
sees, just promises that stop arriving. These series are what turn that
into something alertable.

Two of them are alert lines rather than statistics:

``yuralume_outcome_claim_judge_outage``
    a streak of consecutive judge failures crossed the alarm threshold.
    Non-zero means fulfilments are being withheld because the *gate* is
    broken, not because the text was dishonest.
``yuralume_outcome_claim_blocked_zero_call``
    messages blocked at the exit where the composer called no tool at all
    and claimed an outcome anyway. This is the production defect HV1 was
    opened for, so it is also the number that says whether it is still
    happening — and the rate at which it moves is the honesty-rate metric
    HV3 will build on.

Deliberately mirrors ``action_billing_metrics``: no ``prometheus_client``,
text exposition format 0.0.4, fields enumerated off the dataclass so a
counter added to :class:`OutcomeClaimCounters` appears on the scrape
without anyone remembering to touch this file, and nothing rendered at
all when the gate is not wired (self-host without a judge route).
"""

from __future__ import annotations

from dataclasses import fields, is_dataclass

_PREFIX = "yuralume_outcome_claim_"

_HELP: dict[str, str] = {
    "reviewed": (
        "Messages that reached the honesty judge — background composes "
        "(blocked before delivery) and HV4 chat post-turn audits (judged "
        "after delivery) alike. Split them with chat_audited."
    ),
    "consistent": "Messages the judge cleared to ship unchanged.",
    "consistent_truncated": (
        "S5. Of consistent, the ones the judge only reviewed a prefix of "
        "(message longer than the judge's length cap). Not a block — the "
        "visible prefix read clean and the verdict is unchanged — but a "
        "sustained non-zero rate means the cap in outcome_claim_honesty "
        "is biting often enough on real traffic to be worth raising."
    ),
    "blocked_zero_call": (
        "ALERT LINE. Messages that claimed a completed external action "
        "while the composer had called no tool at all. The 2026-08-25 "
        "production defect; a sustained rise means the prompt-side "
        "honesty rules are losing to the model."
    ),
    "blocked_after_tools": (
        "Messages whose prose overstated what the tools returned (a "
        "search that found nothing written up as a find, a render whose "
        "URL was dropped written up as delivered). S7: also covers the "
        "pass-2 exit's park-that-actually-shipped case — a tool already "
        "produced deliverable attachments, so the gate's fallback line "
        "goes out WITH them rather than the round sending nothing; that "
        "is a delivered-but-blocked round, counted here rather than in "
        "parked."
    ),
    "corrected": (
        "Blocked rounds whose single re-compose came back honest — either "
        "as a real tool call or as text the judge cleared. The gate "
        "working as designed."
    ),
    "parked": (
        "Rounds that sent NOTHING this tick because the re-compose "
        "reoffended or no verdict could be had — the composers' own "
        "retry-next-tick contract, not a promise broken. S7: a round "
        "whose earlier tool call already produced deliverable "
        "attachments does NOT land here even when the honesty gate "
        "blocked the prose, because those attachments still ship with a "
        "fixed fallback line — see blocked_after_tools."
    ),
    "park_retries_exhausted": (
        "ALERT LINE. Promise fulfilments abandoned after the model "
        "re-claimed an unsupported outcome on every allowed retry. Unlike "
        "parked, this is a promise DROPPED rather than deferred — any "
        "sustained non-zero value is an incident."
    ),
    "judge_failed": (
        "Verdicts that could not be produced (call failed, reply "
        "unparseable). Individually ordinary; see the outage line."
    ),
    "judge_outage": (
        "ALERT LINE. Times a run of consecutive judge failures crossed the "
        "alarm threshold. Non-zero means promise fulfilment is being "
        "withheld by a broken gate rather than by dishonest text — check "
        "the outcome_claim_judge feature route before anything else."
    ),
    "chat_audited": (
        "HV4. Chat turns that offered tools and were judged after the "
        "reply had already streamed to the player. The denominator for "
        "the chat-side dishonesty rate."
    ),
    "chat_repair_queued": (
        "HV4. Durable repair follow-ups written after a chat turn was "
        "found dishonest — the character coming back to settle a claim "
        "the stream had already delivered."
    ),
    "chat_repair_coalesced": (
        "F5. Caught lies merged into a repair the conversation already "
        "owed rather than opening a second follow-up row — the claim is "
        "owned either way. Rising towards chat_repair_queued means one "
        "capability is failing repeatedly while the player keeps asking, "
        "not that the model has started overclaiming more often."
    ),
    "chat_repair_missed": (
        "ALERT LINE. A chat turn was judged dishonest and NO repair row "
        "landed (no follow-up store wired, or the write raised). Each one "
        "is an unowned lie, which is exactly what the 100% fulfilment "
        "decision forbids. Any sustained non-zero value is an incident."
    ),
}

#: Series whose sustained non-zero value is an incident, not a data point.
#: Kept as data so an alert-rule generator can read the same list this
#: file documents.
ALERT_FIELDS: frozenset[str] = frozenset({
    _PREFIX + "judge_outage",
    _PREFIX + "blocked_zero_call",
    _PREFIX + "chat_repair_missed",
    _PREFIX + "park_retries_exhausted",
})


def render_outcome_claim_metrics(counters: object | None = None) -> str:
    """Render the honesty-gate counters, or ``""`` when none are wired.

    Takes the *counters* object rather than the guard, so this module
    never learns how the guard is reached from the container. Anything
    that is not a dataclass instance is ignored rather than raised on: a
    metrics scrape must not be the thing that breaks.
    """
    if counters is None or not is_dataclass(counters) or isinstance(counters, type):
        return ""
    lines: list[str] = []
    for field in fields(counters):
        value = getattr(counters, field.name, None)
        if not isinstance(value, int) or isinstance(value, bool):
            continue
        name = f"{_PREFIX}{field.name}"
        lines.append(
            f"# HELP {name} {_HELP.get(field.name, 'Outcome-claim gate counter.')}",
        )
        lines.append(f"# TYPE {name} counter")
        lines.append(f"{name} {value}")
    if not lines:
        return ""
    return "\n".join(lines) + "\n"


__all__ = ["ALERT_FIELDS", "render_outcome_claim_metrics"]
