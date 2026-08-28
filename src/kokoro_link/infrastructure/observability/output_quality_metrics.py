"""Prometheus rendering for the QG player-visible output quality gate.

Like the honesty gate before it, this seam's failure mode is silence — but
it has two silences rather than one, and they need opposite responses.

``yuralume_output_quality_outcomes_total{outcome="hard_skipped"}``
    **ALERT LINE.** A background surface reviewed a message, regenerated
    it, reviewed it again, and still could not ship it — so the tick sent
    nothing. That is the designed behaviour ("寧靜勿爛") and it is also
    indistinguishable, from outside, from a character that has gone quiet.
    Ones and twos are the gate working. A sustained rate means players are
    losing posts and messages, which is the 2026-08-26 incident wearing a
    different hat.

``yuralume_output_quality_failopen_outage_total{surface="..."}``
    **ALERT LINE.** A run of consecutive fail-open reviews on one surface
    crossed the alarm streak. Non-zero means player-visible output on that
    surface is currently shipping **unreviewed** — the gate is not
    blocking anything because it is not answering. Suspect the
    ``novelty_gate`` feature route before suspecting any composer.

The remaining outcomes are statistics, not alerts: ``pass`` is the
denominator, the two ``*_recovered`` series are the gate paying for itself,
and the two ``*_published_best_effort`` series are the deliberate
concessions in the D1 disposal table.

Follows ``outcome_claim_metrics``: no ``prometheus_client``, text exposition
0.0.4, and nothing rendered at all when the gate is unwired. It differs in
one way — the counters carry an open-ended *surface* dimension, so this
renders **labelled** series off a mapping rather than enumerating dataclass
fields. A surface a wave-3 ticket adds therefore appears on the scrape
without anyone touching this file, which is the same property the field
enumeration bought over there.
"""

from __future__ import annotations

from collections.abc import Mapping

_OUTCOMES_SERIES = "yuralume_output_quality_outcomes_total"
_OUTAGE_SERIES = "yuralume_output_quality_failopen_outage_total"

_OUTCOMES_HELP = (
    "Disposals of the player-visible output quality gate, by surface and "
    "outcome. outcome=hard_skipped is an ALERT LINE: a background tick "
    "sent NOTHING because a hard defect survived its regeneration. "
    "outcome=gate_error_failopen means the candidate shipped unreviewed."
)
_OUTAGE_HELP = (
    "ALERT LINE. Times a run of consecutive fail-open reviews on one "
    "surface crossed the alarm streak — player-visible output on that "
    "surface is shipping UNREVIEWED. Check the novelty_gate feature route."
)

#: Series (and, for the outcome series, the label value) whose sustained
#: non-zero movement is an incident rather than a data point. Kept as data
#: so an alert-rule generator can read the same list this file documents.
ALERT_SERIES: frozenset[str] = frozenset({
    _OUTAGE_SERIES,
    f'{_OUTCOMES_SERIES}{{outcome="hard_skipped"}}',
})


def render_output_quality_metrics(counters: object | None = None) -> str:
    """Render the QG counters, or ``""`` when none are wired.

    Takes the counters object rather than the orchestrator, so this module
    never learns how the orchestrator is reached from the container.
    Anything that does not answer the two reader methods is ignored rather
    than raised on, and a counter that cannot be read is skipped rather
    than fatal: a metrics scrape must not be the thing that breaks.
    """
    if counters is None:
        return ""
    lines: list[str] = []
    lines.extend(_render_outcomes(_safe_call(counters, "snapshot")))
    lines.extend(_render_outages(_safe_call(counters, "failopen_outages")))
    if not lines:
        return ""
    return "\n".join(lines) + "\n"


def _render_outcomes(totals: object) -> list[str]:
    rows: list[tuple[str, str, int]] = []
    if isinstance(totals, Mapping):
        for key, value in totals.items():
            if not isinstance(key, tuple) or len(key) != 2:
                continue
            if not isinstance(value, int) or isinstance(value, bool):
                continue
            rows.append((str(key[0]), str(key[1]), value))
    if not rows:
        return []
    rows.sort()
    out = [
        f"# HELP {_OUTCOMES_SERIES} {_OUTCOMES_HELP}",
        f"# TYPE {_OUTCOMES_SERIES} counter",
    ]
    out.extend(
        f'{_OUTCOMES_SERIES}{{surface="{_escape(surface)}",'
        f'outcome="{_escape(outcome)}"}} {value}'
        for surface, outcome, value in rows
    )
    return out


def _render_outages(outages: object) -> list[str]:
    rows: list[tuple[str, int]] = []
    if isinstance(outages, Mapping):
        for key, value in outages.items():
            if not isinstance(value, int) or isinstance(value, bool):
                continue
            rows.append((str(key), value))
    if not rows:
        return []
    rows.sort()
    out = [
        f"# HELP {_OUTAGE_SERIES} {_OUTAGE_HELP}",
        f"# TYPE {_OUTAGE_SERIES} counter",
    ]
    out.extend(
        f'{_OUTAGE_SERIES}{{surface="{_escape(surface)}"}} {value}'
        for surface, value in rows
    )
    return out


def _safe_call(counters: object, name: str) -> object:
    reader = getattr(counters, name, None)
    if not callable(reader):
        return None
    try:
        return reader()
    except Exception:  # noqa: BLE001 - the scrape must never break
        return None


def _escape(value: str) -> str:
    """Escape a Prometheus label value (spec: ``\\``, ``"``, newline)."""
    return (
        value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    )


__all__ = ["ALERT_SERIES", "render_output_quality_metrics"]
