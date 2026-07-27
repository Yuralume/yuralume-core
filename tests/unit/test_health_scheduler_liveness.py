"""Role-aware, scheduler-liveness-aware /health probe (HOSTED_CORE_SCALING §2.1).

A background-role process serves no API, so a static 200 there hides a dead
scheduler from the compose healthcheck + deploy health-wait. These tests pin
the contract: a role that starts schedulers 503s once a started scheduler's
task has exited, while every non-started shape (api role, never-started task,
bare/partial container) stays 200.

The ``started`` / ``is_running`` liveness surface is exercised directly on the
real ProactiveScheduler / WorldEventScheduler, and the /health gate is driven
with lightweight stubs so a completed-task 503 needs no real event loop.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from kokoro_link.api.app import create_app
from kokoro_link.application.services.proactive_scheduler import ProactiveScheduler
from kokoro_link.bootstrap.process_settings import ProcessSettings
from kokoro_link.bootstrap.settings import AppSettings


class _StubScheduler:
    """Minimal object exposing the liveness surface the /health gate reads."""

    def __init__(self, *, started: bool, is_running: bool) -> None:
        self._started = started
        self._is_running = is_running

    @property
    def started(self) -> bool:
        return self._started

    @property
    def is_running(self) -> bool:
        return self._is_running


class _NoopDispatcher:
    async def evaluate(self, *, character_id, trigger, now=None) -> None:
        return None


def _app_for_role(role: str | None):
    process = ProcessSettings(role=role) if role is not None else ProcessSettings()
    # No lifespan is entered by these tests, so the real schedulers never start;
    # the /health gate reads app.state.matrix + app.state.container set at build.
    return create_app(AppSettings(database_url="", process=process))


# --------------------------------------------------------------------------- #
# Scheduler liveness properties (the signal /health consumes)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_proactive_scheduler_liveness_transitions() -> None:
    scheduler = ProactiveScheduler(
        dispatcher=_NoopDispatcher(),  # type: ignore[arg-type]
        character_repository=_EmptyRepo(),  # type: ignore[arg-type]
        tick_seconds=999.0,
        startup_grace_seconds=0.0,
    )
    # Never started → neither started nor running.
    assert scheduler.started is False
    assert scheduler.is_running is False

    await scheduler.start()
    # Started and its task is live.
    assert scheduler.started is True
    assert scheduler.is_running is True

    # A clean stop() nulls the ref → reads as not-started (healthy).
    await scheduler.stop()
    assert scheduler.started is False
    assert scheduler.is_running is False

    # Simulate 'started then died': the background task finished on its own with
    # the ref left in place (stop() was never reached to null it). started stays
    # True while is_running flips False — the exact signal /health 503s on.
    async def _finished() -> None:
        return None

    scheduler._task = asyncio.create_task(_finished())  # noqa: SLF001
    await scheduler._task  # noqa: SLF001 — let it complete
    assert scheduler.started is True
    assert scheduler.is_running is False


# --------------------------------------------------------------------------- #
# /health role gate
# --------------------------------------------------------------------------- #


def test_health_background_role_503_when_scheduler_exited() -> None:
    app = _app_for_role("background")
    app.state.container.proactive_scheduler = _StubScheduler(
        started=True, is_running=False,
    )
    app.state.container.world_event_scheduler = _StubScheduler(
        started=True, is_running=True,
    )
    resp = TestClient(app).get("/health")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "unhealthy"
    assert body["reason"] == "scheduler_exited"


def test_health_background_role_200_when_schedulers_running() -> None:
    app = _app_for_role("background")
    app.state.container.proactive_scheduler = _StubScheduler(
        started=True, is_running=True,
    )
    app.state.container.world_event_scheduler = _StubScheduler(
        started=True, is_running=True,
    )
    resp = TestClient(app).get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_health_api_role_200_even_with_dead_scheduler() -> None:
    # api role starts no schedulers, so a dead scheduler object cannot 503 it.
    app = _app_for_role("api")
    app.state.container.proactive_scheduler = _StubScheduler(
        started=True, is_running=False,
    )
    resp = TestClient(app).get("/health")
    assert resp.status_code == 200


def test_health_default_all_role_200_when_running() -> None:
    app = _app_for_role(None)  # default ProcessSettings → role "all"
    app.state.container.proactive_scheduler = _StubScheduler(
        started=True, is_running=True,
    )
    app.state.container.world_event_scheduler = _StubScheduler(
        started=True, is_running=True,
    )
    resp = TestClient(app).get("/health")
    assert resp.status_code == 200


def test_health_background_role_200_before_start() -> None:
    # Real schedulers wired but never started (no lifespan) → started False → 200.
    app = _app_for_role("background")
    resp = TestClient(app).get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_health_background_role_503_when_shadow_worker_exited() -> None:
    # P2-B: a started-then-died shadow worker must 503 the background process
    # exactly like a dead scheduler.
    app = _app_for_role("background")
    app.state.container.proactive_scheduler = _StubScheduler(
        started=True, is_running=True,
    )
    app.state.container.world_event_scheduler = _StubScheduler(
        started=True, is_running=True,
    )
    app.state.container.background_shadow_coordinator = _StubScheduler(
        started=True, is_running=True,
    )
    app.state.container.background_shadow_worker = _StubScheduler(
        started=True, is_running=False,
    )
    resp = TestClient(app).get("/health")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "unhealthy"
    assert body["reason"] == "scheduler_exited"


def test_health_background_role_200_when_shadow_services_running() -> None:
    app = _app_for_role("background")
    app.state.container.proactive_scheduler = _StubScheduler(
        started=True, is_running=True,
    )
    app.state.container.world_event_scheduler = _StubScheduler(
        started=True, is_running=True,
    )
    app.state.container.background_shadow_coordinator = _StubScheduler(
        started=True, is_running=True,
    )
    app.state.container.background_shadow_worker = _StubScheduler(
        started=True, is_running=True,
    )
    resp = TestClient(app).get("/health")
    assert resp.status_code == 200


def test_health_coordinator_role_503_when_coordinator_exited() -> None:
    # §2.1 dedicated coordinator: 503s on ITS loop's death, with no dependence on
    # an embedded scheduler it never runs.
    app = _app_for_role("coordinator")
    app.state.container.background_shadow_coordinator = _StubScheduler(
        started=True, is_running=False,
    )
    resp = TestClient(app).get("/health")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "unhealthy"
    assert body["reason"] == "scheduler_exited"


def test_health_coordinator_role_ignores_dead_worker() -> None:
    # A coordinator process owns no worker loop → a dead worker object cannot 503
    # it (a worker in a different process is not this process's health).
    app = _app_for_role("coordinator")
    app.state.container.background_shadow_coordinator = _StubScheduler(
        started=True, is_running=True,
    )
    app.state.container.background_shadow_worker = _StubScheduler(
        started=True, is_running=False,
    )
    resp = TestClient(app).get("/health")
    assert resp.status_code == 200


def test_health_worker_role_503_when_worker_exited() -> None:
    app = _app_for_role("worker")
    app.state.container.background_shadow_worker = _StubScheduler(
        started=True, is_running=False,
    )
    resp = TestClient(app).get("/health")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "unhealthy"
    assert body["reason"] == "scheduler_exited"


def test_health_worker_role_200_when_worker_running() -> None:
    app = _app_for_role("worker")
    app.state.container.background_shadow_worker = _StubScheduler(
        started=True, is_running=True,
    )
    resp = TestClient(app).get("/health")
    assert resp.status_code == 200


def test_health_connector_role_ignores_dead_background_loops() -> None:
    # A connector process owns neither coordinator nor worker → dead objects of
    # those cannot 503 it.
    app = _app_for_role("connector")
    app.state.container.background_shadow_coordinator = _StubScheduler(
        started=True, is_running=False,
    )
    app.state.container.background_shadow_worker = _StubScheduler(
        started=True, is_running=False,
    )
    resp = TestClient(app).get("/health")
    assert resp.status_code == 200


class _EmptyRepo:
    async def list_active(self):
        return []

    async def list(self):
        return []

    async def get(self, character_id):
        return None
