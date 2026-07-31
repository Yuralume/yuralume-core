"""Action-billing counters on the internal metrics endpoint (AP5, residual R-2).

The fixed-price billing path already counted everything §AP5 asks for; until
now those integers never left the process. These tests pin the three promises
of exposing them:

1. every field of ``ActionBillingCounters`` / ``OverageCounters`` reaches the
   scrape (enumerated from the dataclass, so a new counter cannot be forgotten);
2. the two alert lines are labelled as such in their HELP text, because a
   number nobody knows to page on is not an alert;
3. a deployment with no action billing wired (self-host) renders exactly what
   it rendered before.
"""

from __future__ import annotations

from dataclasses import fields

import pytest
from fastapi.testclient import TestClient

from kokoro_link.api.app import create_app
from kokoro_link.application.services.cloud_action_billing_service import (
    ActionBillingCounters,
)
from kokoro_link.application.services.quota_overage_service import (
    OverageCounters,
)
from kokoro_link.bootstrap.process_settings import ProcessSettings
from kokoro_link.bootstrap.settings import AppSettings
from kokoro_link.infrastructure.observability.action_billing_metrics import (
    render_action_billing_metrics,
)

pytestmark = pytest.mark.asyncio

_METRICS_PATH = "/api/internal/v1/metrics"
_METRICS_ENV = "YURALUME_METRICS_INTERNAL_TOKEN"
_TOKEN = "scrape-secret"

_BILLING_PREFIX = "yuralume_action_billing_"
_OVERAGE_PREFIX = "yuralume_quota_overage_"


class _CountersHolder:
    """Stands in for the services — the exporter only wants ``.counters``."""

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


async def test_every_billing_counter_field_is_exported(monkeypatch) -> None:
    app = _app(monkeypatch)
    counters = ActionBillingCounters(
        charged=7, released_uncovered=2, close_abandoned=1,
    )
    app.state.container.action_billing_service = _CountersHolder(counters)

    body = _scrape(app)

    for field in fields(ActionBillingCounters):
        # The whole point of enumerating the dataclass: adding a counter must
        # not require remembering this file.
        assert f"{_BILLING_PREFIX}{field.name} " in body
    assert f"{_BILLING_PREFIX}charged 7" in body
    assert f"{_BILLING_PREFIX}released_uncovered 2" in body
    assert f"{_BILLING_PREFIX}close_abandoned 1" in body
    # The pre-existing series are untouched.
    assert "yuralume_core_scheduler_ticks_total" in body


async def test_overage_counters_are_exported(monkeypatch) -> None:
    app = _app(monkeypatch)
    # Overage rides the billing block: it can only move on a fixed-price tier,
    # which is exactly when the billing counters exist.
    app.state.container.action_billing_service = _CountersHolder(
        ActionBillingCounters(),
    )
    app.state.container.quota_overage_service = _CountersHolder(
        OverageCounters(price_changed=3, consent_revoked=3),
    )

    body = _scrape(app)

    for field in fields(OverageCounters):
        assert f"{_OVERAGE_PREFIX}{field.name} " in body
    assert f"{_OVERAGE_PREFIX}price_changed 3" in body


async def test_alert_lines_say_they_are_alert_lines(monkeypatch) -> None:
    # Both series mean "money is being handled wrongly"; the scrape has to say
    # so, or the number reads as another counter to ignore.
    app = _app(monkeypatch)
    app.state.container.action_billing_service = _CountersHolder(
        ActionBillingCounters(),
    )

    body = _scrape(app)

    for alert in ("released_uncovered", "close_abandoned"):
        help_line = next(
            line for line in body.splitlines()
            if line.startswith(f"# HELP {_BILLING_PREFIX}{alert} ")
        )
        assert "ALERT LINE" in help_line


async def test_absent_when_nothing_is_wired(monkeypatch) -> None:
    # Self-host shape: the null billing service owns no counters, so not one
    # of these series may appear — including the overage pair, whose service
    # *is* wired there but can never move without fixed-price billing.
    app = _app(monkeypatch)

    body = _scrape(app)

    assert _BILLING_PREFIX not in body
    assert _OVERAGE_PREFIX not in body
    assert "yuralume_core_scheduler_ticks_total" in body


async def test_renderer_ignores_a_non_dataclass_counter_bag() -> None:
    # A stub service in some other test suite must degrade to silence, never
    # to a 500 on the scrape.
    assert render_action_billing_metrics(billing=object(), overage=None) == ""
    assert render_action_billing_metrics() == ""
    # Overage alone is not a reason to render: see the gate in the renderer.
    assert render_action_billing_metrics(overage=OverageCounters()) == ""


async def test_renderer_declares_the_prometheus_type() -> None:
    body = render_action_billing_metrics(billing=ActionBillingCounters(charged=1))

    assert f"# TYPE {_BILLING_PREFIX}charged counter" in body
    assert body.endswith("\n")
