"""Phase 0 scheduler metrics (HOSTED_CORE_SCALING_ARCHITECTURE_PLAN §12).

Covers the registry's Prometheus rendering (metric names, HELP/TYPE lines,
label escaping, pre-first-tick shape, counter monotonicity) and the internal
scrape route (503 unconfigured, 401 bad/missing bearer, 200 + body with the
token), plus role coverage: both the ``api`` and ``background`` roles serve
``/api/internal/v1/metrics`` while ``background`` still 404s ``/api/v1``.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from kokoro_link.api.app import create_app
from kokoro_link.application.services.proactive_scheduler import ProactiveScheduler
from kokoro_link.bootstrap.process_settings import ProcessSettings
from kokoro_link.bootstrap.settings import AppSettings
from kokoro_link.infrastructure.observability.scheduler_metrics import (
    SchedulerMetrics,
)

_METRICS_ENV = "YURALUME_METRICS_INTERNAL_TOKEN"
_METRICS_PATH = "/api/internal/v1/metrics"


# --------------------------------------------------------------------------- #
# Registry rendering
# --------------------------------------------------------------------------- #


def test_render_before_first_tick_only_counters_and_build_info() -> None:
    body = SchedulerMetrics().render_prometheus()

    # Both counters start at 0 with proper HELP/TYPE lines.
    assert "# HELP yuralume_core_scheduler_ticks_total" in body
    assert "# TYPE yuralume_core_scheduler_ticks_total counter" in body
    assert "yuralume_core_scheduler_ticks_total 0" in body
    assert "# TYPE yuralume_core_scheduler_tick_failures_total counter" in body
    assert "yuralume_core_scheduler_tick_failures_total 0" in body

    # build_info is always present.
    assert "# TYPE yuralume_core_build_info gauge" in body
    assert "yuralume_core_build_info{version=" in body
    assert body.rstrip().endswith("} 1")

    # Last-tick gauges must be ABSENT before any successful tick.
    assert "yuralume_core_scheduler_tick_duration_seconds" not in body
    assert (
        "yuralume_core_scheduler_last_successful_tick_timestamp_seconds"
        not in body
    )
    assert "yuralume_core_characters" not in body


def test_render_after_tick_includes_all_series_with_help_type() -> None:
    metrics = SchedulerMetrics()
    metrics.record_tick(
        duration_seconds=0.25,
        step_durations={"demo_reaper": 0.01, "proactive_dispatch": 0.2},
        characters_active=7,
        characters_frozen=2,
        characters_total=9,
    )
    body = metrics.render_prometheus()

    for name, kind in (
        ("yuralume_core_scheduler_tick_duration_seconds", "gauge"),
        ("yuralume_core_scheduler_tick_step_duration_seconds", "gauge"),
        ("yuralume_core_scheduler_last_successful_tick_timestamp_seconds", "gauge"),
        ("yuralume_core_characters", "gauge"),
    ):
        assert f"# HELP {name}" in body
        assert f"# TYPE {name} {kind}" in body

    assert "yuralume_core_scheduler_tick_duration_seconds 0.25" in body
    assert (
        'yuralume_core_scheduler_tick_step_duration_seconds{step="demo_reaper"} 0.01'
        in body
    )
    assert 'yuralume_core_characters{state="active"} 7' in body
    assert 'yuralume_core_characters{state="frozen"} 2' in body
    assert 'yuralume_core_characters{state="total"} 9' in body


def test_frozen_total_gauges_omitted_when_unknown() -> None:
    metrics = SchedulerMetrics()
    metrics.record_tick(
        duration_seconds=0.1,
        step_durations={},
        characters_active=3,
        characters_frozen=None,
        characters_total=None,
    )
    body = metrics.render_prometheus()
    assert 'yuralume_core_characters{state="active"} 3' in body
    # A repo failure records active only — frozen/total absent, not 0.
    assert 'state="frozen"' not in body
    assert 'state="total"' not in body


def test_label_values_are_escaped() -> None:
    metrics = SchedulerMetrics()
    metrics.record_tick(
        duration_seconds=0.1,
        # backslash, double-quote, newline must all be escaped.
        step_durations={'a\\b"c\nd': 0.5},
        characters_active=1,
    )
    body = metrics.render_prometheus()
    assert 'step="a\\\\b\\"c\\nd"' in body


def test_ticks_total_is_monotonic() -> None:
    metrics = SchedulerMetrics()
    assert "yuralume_core_scheduler_ticks_total 0" in metrics.render_prometheus()
    metrics.record_tick(
        duration_seconds=0.1, step_durations={}, characters_active=0,
    )
    assert "yuralume_core_scheduler_ticks_total 1" in metrics.render_prometheus()
    metrics.record_tick(
        duration_seconds=0.1, step_durations={}, characters_active=0,
    )
    assert "yuralume_core_scheduler_ticks_total 2" in metrics.render_prometheus()


def test_record_tick_failure_counts_attempt_but_freezes_success_gauges() -> None:
    metrics = SchedulerMetrics()
    metrics.record_tick(
        duration_seconds=0.2,
        step_durations={"list_characters": 0.05},
        characters_active=6,
        characters_frozen=1,
        characters_total=7,
    )
    ts_after_success = metrics.last_successful_tick_at
    assert metrics.ticks_total == 1
    assert metrics.tick_failures_total == 0
    assert ts_after_success > 0

    metrics.record_tick_failure()
    # Attempt + failure counters advance...
    assert metrics.ticks_total == 2
    assert metrics.tick_failures_total == 1
    # ...but every last-successful gauge is frozen (this is what lets the
    # last-successful-tick staleness alert fire while ticks keep failing).
    assert metrics.last_successful_tick_at == ts_after_success
    assert metrics.characters_active == 6
    assert metrics.last_tick_duration_seconds == 0.2

    body = metrics.render_prometheus()
    assert "yuralume_core_scheduler_ticks_total 2" in body
    assert "yuralume_core_scheduler_tick_failures_total 1" in body


# --------------------------------------------------------------------------- #
# Scheduler-driven accounting: a failed tick vs a successful one
# --------------------------------------------------------------------------- #


class _FlakyRepo:
    """Character repo double whose ``list_active`` can be flipped to raise, to
    drive a real ProactiveScheduler tick down the failure path."""

    def __init__(self) -> None:
        self.explode = False

    async def list_active(self):
        if self.explode:
            raise RuntimeError("db unavailable")
        return []

    async def list(self):
        return []

    async def get(self, character_id):
        return None


class _NoopDispatcher:
    async def evaluate(self, *, character_id, trigger, now=None) -> None:
        return None


@pytest.mark.asyncio
async def test_tick_whose_character_listing_raises_records_a_failure() -> None:
    metrics = SchedulerMetrics()
    repo = _FlakyRepo()
    scheduler = ProactiveScheduler(
        dispatcher=_NoopDispatcher(),  # type: ignore[arg-type]
        character_repository=repo,  # type: ignore[arg-type]
        tick_seconds=999.0,
        startup_grace_seconds=0.0,
        metrics=metrics,
    )

    # First tick succeeds (empty working set) → sets the success baseline.
    await scheduler._tick_all()  # noqa: SLF001 — direct drive is the test seam
    assert metrics.ticks_total == 1
    assert metrics.tick_failures_total == 0
    ts_before = metrics.last_successful_tick_at
    assert ts_before > 0

    # Second tick: the character listing raises. The attempt is counted and the
    # failure counter increments, but the last-successful timestamp is frozen.
    repo.explode = True
    await scheduler._tick_all()  # noqa: SLF001
    assert metrics.ticks_total == 2
    assert metrics.tick_failures_total == 1
    assert metrics.last_successful_tick_at == ts_before


# --------------------------------------------------------------------------- #
# Scrape route
# --------------------------------------------------------------------------- #


def _app_for_role(role: str) -> object:
    # No lifespan context is entered, so the real schedulers/DB never start —
    # the route only reads app.state.container.scheduler_metrics, set at build.
    return create_app(
        AppSettings(database_url="", process=ProcessSettings(role=role)),
    )


def test_route_503_when_token_unset(monkeypatch) -> None:
    monkeypatch.delenv(_METRICS_ENV, raising=False)
    app = _app_for_role("api")
    with TestClient(app) as client:
        resp = client.get(
            _METRICS_PATH, headers={"Authorization": "Bearer whatever"},
        )
    assert resp.status_code == 503


def test_route_401_on_wrong_bearer(monkeypatch) -> None:
    monkeypatch.setenv(_METRICS_ENV, "scrape-secret")
    app = _app_for_role("api")
    with TestClient(app) as client:
        resp = client.get(
            _METRICS_PATH, headers={"Authorization": "Bearer wrong"},
        )
    assert resp.status_code == 401


def test_route_401_on_missing_bearer(monkeypatch) -> None:
    monkeypatch.setenv(_METRICS_ENV, "scrape-secret")
    app = _app_for_role("api")
    with TestClient(app) as client:
        resp = client.get(_METRICS_PATH)
    assert resp.status_code == 401


def test_route_200_with_token_returns_prometheus_body(monkeypatch) -> None:
    monkeypatch.setenv(_METRICS_ENV, "scrape-secret")
    app = _app_for_role("api")
    # Seed a recorded tick so the body carries the full series.
    app.state.container.scheduler_metrics.record_tick(
        duration_seconds=0.3,
        step_durations={"proactive_dispatch": 0.25},
        characters_active=4,
        characters_frozen=1,
        characters_total=5,
    )
    with TestClient(app) as client:
        resp = client.get(
            _METRICS_PATH, headers={"Authorization": "Bearer scrape-secret"},
        )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert "version=0.0.4" in resp.headers["content-type"]
    body = resp.text
    assert "yuralume_core_scheduler_ticks_total 1" in body
    assert 'yuralume_core_characters{state="active"} 4' in body
    assert (
        'yuralume_core_scheduler_tick_step_duration_seconds'
        '{step="proactive_dispatch"} 0.25' in body
    )


# --------------------------------------------------------------------------- #
# Role coverage
# --------------------------------------------------------------------------- #


def test_background_role_serves_metrics_but_404s_api(monkeypatch) -> None:
    monkeypatch.setenv(_METRICS_ENV, "scrape-secret")
    app = _app_for_role("background")
    with TestClient(app) as client:
        metrics_resp = client.get(
            _METRICS_PATH, headers={"Authorization": "Bearer scrape-secret"},
        )
        api_resp = client.get("/api/v1/system/version")
    # Metrics route is mounted in the headless background role...
    assert metrics_resp.status_code == 200
    assert "yuralume_core_scheduler_ticks_total" in metrics_resp.text
    # ...but the public API surface is still absent.
    assert api_resp.status_code == 404


def test_api_role_serves_metrics(monkeypatch) -> None:
    monkeypatch.setenv(_METRICS_ENV, "scrape-secret")
    app = _app_for_role("api")
    with TestClient(app) as client:
        resp = client.get(
            _METRICS_PATH, headers={"Authorization": "Bearer scrape-secret"},
        )
    assert resp.status_code == 200
    assert "yuralume_core_build_info" in resp.text
