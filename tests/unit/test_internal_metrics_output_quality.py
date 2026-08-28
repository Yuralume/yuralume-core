"""QG0 output-quality counters on the internal metrics endpoint.

``render_output_quality_metrics`` has its own coverage next door; what this
file pins is the wiring — ``internal_metrics._render_output_quality_metrics``
reading ``container.output_quality_counters`` and appending it to the real
scrape, without disturbing the series already riding that endpoint.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from kokoro_link.api.app import create_app
from kokoro_link.application.services.output_quality import (
    OUTCOME_GATE_ERROR_FAILOPEN,
    OUTCOME_HARD_SKIPPED,
    OUTCOME_PASS,
    OutputQualityCounters,
)
from kokoro_link.bootstrap.process_settings import ProcessSettings
from kokoro_link.bootstrap.settings import AppSettings

pytestmark = pytest.mark.asyncio

_METRICS_PATH = "/api/internal/v1/metrics"
_METRICS_ENV = "YURALUME_METRICS_INTERNAL_TOKEN"
_TOKEN = "scrape-secret"
_PREFIX = "yuralume_output_quality_"


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


async def test_recorded_outcomes_reach_the_scrape(monkeypatch) -> None:
    app = _app(monkeypatch)
    counters = app.state.container.output_quality_counters
    assert isinstance(counters, OutputQualityCounters)
    counters.record("feed", OUTCOME_PASS)
    counters.record("feed", OUTCOME_HARD_SKIPPED)

    body = _scrape(app)

    assert f'{_PREFIX}outcomes_total{{surface="feed",outcome="pass"}} 1' in body
    assert (
        f'{_PREFIX}outcomes_total{{surface="feed",outcome="hard_skipped"}} 1'
        in body
    )
    # The pre-existing series are untouched.
    assert "yuralume_core_scheduler_ticks_total" in body


async def test_the_outage_alert_line_says_it_is_one(monkeypatch) -> None:
    app = _app(monkeypatch)
    counters = app.state.container.output_quality_counters
    for _ in range(5):
        counters.record("proactive", OUTCOME_GATE_ERROR_FAILOPEN)

    body = _scrape(app)

    assert f'{_PREFIX}failopen_outage_total{{surface="proactive"}} 1' in body
    help_line = next(
        line for line in body.splitlines()
        if line.startswith(f"# HELP {_PREFIX}failopen_outage_total ")
    )
    assert "ALERT LINE" in help_line


async def test_absent_until_something_has_been_reviewed(monkeypatch) -> None:
    """Zero-valued series would claim surfaces this deployment may never
    run. Nothing reviewed, nothing rendered."""
    app = _app(monkeypatch)

    body = _scrape(app)

    assert _PREFIX not in body
    assert "yuralume_core_scheduler_ticks_total" in body


async def test_absent_when_the_field_is_unwired(monkeypatch) -> None:
    app = _app(monkeypatch)
    app.state.container.output_quality_counters = None

    body = _scrape(app)

    assert _PREFIX not in body
    assert "yuralume_core_scheduler_ticks_total" in body


async def test_a_counter_read_failure_never_breaks_the_scrape(
    monkeypatch,
) -> None:
    """This endpoint carries the drain gauge ``deploy.sh`` polls to zero
    before recreating a replica, so a broken counter here must degrade to
    silence rather than stall a rolling deploy."""
    class _Raising:
        def snapshot(self):
            raise RuntimeError("counters unavailable")

        def failopen_outages(self):
            raise RuntimeError("counters unavailable")

    app = _app(monkeypatch)
    app.state.container.output_quality_counters = _Raising()

    body = _scrape(app)

    assert _PREFIX not in body
    assert "yuralume_core_scheduler_ticks_total" in body
