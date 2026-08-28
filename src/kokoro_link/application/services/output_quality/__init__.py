"""Shared review→regenerate→dispose band for player-visible output (QG).

Every surface that shows a player generated prose already asks the same
three questions — "is this good enough to send", "if not, what do I tell
the model to fix", "and if the second try is still bad, do I send it or
send nothing" — and before QG each of them answered in its own hand-rolled
way. Nine axes of verdict, two fail levels and two surface policies is more
disposal logic than any one composer should carry, and the versions that
drifted apart were exactly the ones that shipped the 2026-08-26 defect.

So the band lives here, once:

:mod:`~kokoro_link.application.services.output_quality.orchestrator`
    :class:`OutputQualityOrchestrator` — the D7 编排 itself. Surface
    agnostic: callers hand it two callbacks (build the gate context,
    regenerate with feedback) and a policy, and get back a decision.
:mod:`~kokoro_link.application.services.output_quality.counters`
    process-lifetime totals per ``(surface, outcome)``, plus the
    fail-open outage streak alarm (the HV ``judge_outage`` pattern).
:mod:`~kokoro_link.application.services.output_quality.evidence`
    deterministic *evidence* helpers. They compute and describe; they
    never intercept. The verdict stays with the judge (D6 / LLM-first).
"""

from __future__ import annotations

from kokoro_link.application.services.output_quality.counters import (
    DEFAULT_FAILOPEN_ALARM_STREAK,
    OutputQualityCounters,
)
from kokoro_link.application.services.output_quality.evidence import (
    length_overrun_lines,
    script_mix_lines,
)
from kokoro_link.application.services.output_quality.orchestrator import (
    ALL_OUTCOMES,
    OUTCOME_GATE_ERROR_FAILOPEN,
    OUTCOME_HARD_DEGRADED,
    OUTCOME_HARD_PUBLISHED_BEST_EFFORT,
    OUTCOME_HARD_RECOVERED,
    OUTCOME_HARD_SKIPPED,
    OUTCOME_PASS,
    OUTCOME_SOFT_PUBLISHED_BEST_EFFORT,
    OUTCOME_SOFT_RECOVERED,
    OutputQualityOrchestrator,
    OutputQualityPolicy,
    OutputQualityReview,
    fired_axes,
)

__all__ = [
    "ALL_OUTCOMES",
    "DEFAULT_FAILOPEN_ALARM_STREAK",
    "OUTCOME_GATE_ERROR_FAILOPEN",
    "OUTCOME_HARD_DEGRADED",
    "OUTCOME_HARD_PUBLISHED_BEST_EFFORT",
    "OUTCOME_HARD_RECOVERED",
    "OUTCOME_HARD_SKIPPED",
    "OUTCOME_PASS",
    "OUTCOME_SOFT_PUBLISHED_BEST_EFFORT",
    "OUTCOME_SOFT_RECOVERED",
    "OutputQualityCounters",
    "OutputQualityOrchestrator",
    "OutputQualityPolicy",
    "OutputQualityReview",
    "fired_axes",
    "length_overrun_lines",
    "script_mix_lines",
]
