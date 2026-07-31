"""Prometheus rendering for the AP2/AP4 action-billing counters (AP5).

``CloudActionBillingService.counters`` and ``QuotaOverageService.counters``
already record everything the reconciliation story in
``CREDIT_ACTION_PRICING_PLAN`` §AP5 needs — they were simply trapped in the
process. This module lifts them onto the existing internal metrics scrape so a
fixed-price deployment has a trend line instead of a log grep.

Deliberately mirrors ``execution_mode_metrics``: no ``prometheus_client``, text
exposition format 0.0.4, and nothing is rendered when the collaborator is not
wired (self-host runs the null billing service, which owns no counters, so the
series never appear).

Field names are taken from the dataclasses themselves rather than a hand-kept
list: a counter added to :class:`ActionBillingCounters` must show up on the
scrape without anyone remembering to touch this file.

**Alert lines** (see :data:`_ALERT_FIELDS`): ``released_uncovered`` and
``close_abandoned`` are not statistics. Any sustained value above zero means
money is being handled wrongly — see their HELP text and the module docstring
of ``cloud_action_billing_service``.
"""

from __future__ import annotations

from dataclasses import fields, is_dataclass

_BILLING_PREFIX = "yuralume_action_billing_"
_OVERAGE_PREFIX = "yuralume_quota_overage_"

#: One line each, so a scrape is self-documenting. A field with no entry here
#: still renders — with a generic HELP — because the exporter enumerates the
#: dataclass, not this table.
_BILLING_HELP: dict[str, str] = {
    "charged": "Actions that reserved their fixed price successfully.",
    "no_price": (
        "Actions whose (tier, action_key) has no active price. Deliberately "
        "fail-open: the action runs covered and free, and the User service "
        "raises its own ACTION_PRICE_MISSING alert."
    ),
    "charge_failed": (
        "Charges swallowed because the wallet service was unreachable. The "
        "action ran uncharged — revenue under-counted, play never blocked."
    ),
    "settle_failed": "Settle calls that failed on the request path.",
    "release_failed": "Release calls that failed on the request path.",
    "refused": (
        "Charges refused by the wallet as a decision, not an outage "
        "(insufficient credits)."
    ),
    "price_changed": (
        "Charges refused because the quoted price no longer matched. A rising "
        "number means the back office is being edited under live sessions."
    ),
    "close_retried": "Settle/release attempts queued for off-path retry.",
    "close_recovered": "Retried closes that eventually landed.",
    "close_abandoned": (
        "ALERT LINE. Reservations Core could never close: each one is money "
        "held on a player's wallet until the User-side sweeper resolves it."
    ),
    "settled_after_use": (
        "Actions that failed after the Gateway had already waived a call, so "
        "they settled instead of refunding a generation already served."
    ),
    "released_uncovered": (
        "ALERT LINE. Actions that succeeded without a single covered Gateway "
        "call, so the fixed charge was refunded to avoid double billing. A "
        "rising number means the Gateway is not honouring Core's interaction "
        "header (version skew) and the fixed-price tier is silently running "
        "on per-call billing."
    ),
}

_OVERAGE_HELP: dict[str, str] = {
    "price_changed": (
        "Overage purchases refused because the price outgrew the player's "
        "recorded consent."
    ),
    "consent_revoked": (
        "Overage switches turned back off by that refusal. The background "
        "half of AP4 denies silently, so this is the only population-level "
        "signal that a price rise is switching consent off."
    ),
}

#: Series whose non-zero value is an incident, not a data point. Kept as data
#: so an alert-rule generator can read the same list this file documents.
_ALERT_FIELDS: frozenset[str] = frozenset({
    _BILLING_PREFIX + "released_uncovered",
    _BILLING_PREFIX + "close_abandoned",
})


def render_action_billing_metrics(
    *, billing: object | None = None, overage: object | None = None,
) -> str:
    """Render the action-billing counters, or ``""`` when none are wired.

    ``billing`` / ``overage`` are the *counter* objects (not the services), so
    this module never has to know how they are reached from the container.
    Anything that is not a dataclass instance is ignored rather than raised on:
    a metrics scrape must not be the thing that breaks.

    The billing counters gate the whole block, overage included. Self-host runs
    the null billing service (which owns no counters) while still wiring
    :class:`QuotaOverageService`, whose counters can only ever move on a
    fixed-price tier — rendering them alone would put a permanently-zero hosted
    series on every self-host scrape for nothing.
    """
    billing_lines = _render_counters(billing, _BILLING_PREFIX, _BILLING_HELP)
    if not billing_lines:
        return ""
    lines: list[str] = list(billing_lines)
    lines.extend(_render_counters(overage, _OVERAGE_PREFIX, _OVERAGE_HELP))
    if not lines:
        return ""
    return "\n".join(lines) + "\n"


def _render_counters(
    counters: object | None, prefix: str, help_text: dict[str, str],
) -> list[str]:
    if counters is None or not is_dataclass(counters) or isinstance(counters, type):
        return []
    lines: list[str] = []
    for field in fields(counters):
        value = getattr(counters, field.name, None)
        if not isinstance(value, int) or isinstance(value, bool):
            continue
        name = f"{prefix}{field.name}"
        lines.append(
            f"# HELP {name} "
            f"{help_text.get(field.name, 'Action billing counter.')}",
        )
        lines.append(f"# TYPE {name} counter")
        lines.append(f"{name} {value}")
    return lines
