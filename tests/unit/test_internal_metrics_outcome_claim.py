"""HV1 outcome-claim counters on the internal metrics endpoint (HV3).

``render_outcome_claim_metrics`` itself already has direct coverage in
``test_outcome_claim_judge.py``; what has never been exercised is the
route wiring — ``internal_metrics._render_outcome_claim_metrics`` reading
``container.outcome_claim_guard.counters`` and appending it to the real
``/metrics`` scrape. Mirrors ``test_internal_metrics_action_billing.py``'s
shape exactly, including the "absent when unwired" and "alert lines say
so" checks, which is what makes the 謊稱率 (dishonesty rate) this ticket
is about actually *observable* rather than merely computed in-process.
"""

from __future__ import annotations

from dataclasses import fields

import pytest
from fastapi.testclient import TestClient

from kokoro_link.api.app import create_app
from kokoro_link.application.services.outcome_claim_guard import (
    OutcomeClaimCounters,
)
from kokoro_link.bootstrap.process_settings import ProcessSettings
from kokoro_link.bootstrap.settings import AppSettings
from kokoro_link.infrastructure.observability.outcome_claim_metrics import (
    ALERT_FIELDS,
)

pytestmark = pytest.mark.asyncio

_METRICS_PATH = "/api/internal/v1/metrics"
_METRICS_ENV = "YURALUME_METRICS_INTERNAL_TOKEN"
_TOKEN = "scrape-secret"

_PREFIX = "yuralume_outcome_claim_"


class _GuardHolder:
    """Stands in for ``OutcomeClaimGuard`` — the exporter only reads
    ``.counters``."""

    def __init__(self, counters: object) -> None:
        self.counters = counters


def _app(monkeypatch):
    monkeypatch.setenv(_METRICS_ENV, _TOKEN)
    return create_app(
        AppSettings(database_url="", process=ProcessSettings(role="api")),
    )


def _scrape(app) -> str:
    with TestClient(app) as client:
        resp = client.get(
            _METRICS_PATH, headers={"Authorization": f"Bearer {_TOKEN}"},
        )
    assert resp.status_code == 200
    return resp.text


async def test_every_counter_field_is_exported(monkeypatch) -> None:
    app = _app(monkeypatch)
    counters = OutcomeClaimCounters(
        reviewed=10, consistent=6, blocked_zero_call=2, blocked_after_tools=1,
        corrected=2, parked=1, judge_failed=1, judge_outage=0,
    )
    app.state.container.outcome_claim_guard = _GuardHolder(counters)

    body = _scrape(app)

    for field in fields(OutcomeClaimCounters):
        # The point of enumerating the dataclass: a counter added later
        # must not require remembering this file.
        assert f"{_PREFIX}{field.name} " in body
    assert f"{_PREFIX}reviewed 10" in body
    assert f"{_PREFIX}blocked_zero_call 2" in body
    assert f"{_PREFIX}corrected 2" in body
    assert f"{_PREFIX}parked 1" in body
    # The pre-existing series are untouched.
    assert "yuralume_core_scheduler_ticks_total" in body


async def test_alert_lines_say_they_are_alert_lines(monkeypatch) -> None:
    """``blocked_zero_call`` and ``judge_outage`` are the two numbers this
    ticket exists to make pageable — a number nobody knows to alert on is
    just a counter nobody reads."""
    app = _app(monkeypatch)
    app.state.container.outcome_claim_guard = _GuardHolder(
        OutcomeClaimCounters(),
    )

    body = _scrape(app)

    for series in ALERT_FIELDS:
        name = series.removeprefix(_PREFIX)
        help_line = next(
            line for line in body.splitlines()
            if line.startswith(f"# HELP {_PREFIX}{name} ")
        )
        assert "ALERT LINE" in help_line


async def test_absent_when_the_gate_is_unwired(monkeypatch) -> None:
    """A deployment with no judge route (``outcome_claim_guard is None``,
    the renderer's own contract): not one series may appear. The default
    test container always builds a guard, so this pins the *renderer's*
    behaviour rather than today's wiring — it must still degrade cleanly
    if a future role ever leaves the field unset."""
    app = _app(monkeypatch)
    app.state.container.outcome_claim_guard = None

    body = _scrape(app)

    assert _PREFIX not in body
    assert "yuralume_core_scheduler_ticks_total" in body


async def test_wired_by_default_with_zero_counters(monkeypatch) -> None:
    """The guard is built unconditionally by the container (HV1) — every
    process role's scrape carries the series from process start, at zero,
    which is what makes a later non-zero reading meaningful rather than
    "series just appeared"."""
    app = _app(monkeypatch)

    body = _scrape(app)

    assert f"{_PREFIX}reviewed 0" in body
    assert f"{_PREFIX}blocked_zero_call 0" in body


async def test_a_counter_read_failure_never_breaks_the_scrape(
    monkeypatch,
) -> None:
    """A guard whose ``.counters`` raises (a future refactor, a broken
    stand-in in some other test's fixture) must degrade to silence, never
    a 500 on the shared scrape every other series rides on."""
    class _RaisingGuard:
        @property
        def counters(self):
            raise RuntimeError("counters unavailable")

    app = _app(monkeypatch)
    app.state.container.outcome_claim_guard = _RaisingGuard()

    body = _scrape(app)

    assert _PREFIX not in body
    assert "yuralume_core_scheduler_ticks_total" in body
