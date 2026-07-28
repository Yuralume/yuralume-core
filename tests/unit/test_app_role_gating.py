"""Phase 1 lifespan + router gating by process role (HOSTED_CORE_SCALING §2.1).

Asserts that ``create_app`` honours the component matrix: the ``api`` role
serves routes + runs studio recovery but starts no schedulers/connectors; the
``background`` role serves only ``/health`` while starting schedulers and
skipping studio recovery; and the default ``all`` role preserves current
behaviour. Scheduler / recovery objects are replaced with recording fakes so
the assertions don't depend on real background work.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from kokoro_link.api.app import create_app
from kokoro_link.bootstrap.process_settings import ProcessSettings
from kokoro_link.bootstrap.settings import AppSettings


class _FakeScheduler:
    def __init__(self) -> None:
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True


class _FakeExternalChatTurns:
    def __init__(self) -> None:
        self.stop_calls = 0

    async def stop(self) -> None:
        self.stop_calls += 1


class _FakeRecovery:
    def __init__(self) -> None:
        self.called = False

    async def recover(self) -> dict:
        self.called = True
        return {}


def _install_fakes(container):
    proactive = _FakeScheduler()
    world = _FakeScheduler()
    telegram = _FakeScheduler()
    studio = _FakeRecovery()
    container.proactive_scheduler = proactive
    container.world_event_scheduler = world
    container.telegram_polling_service = telegram
    container.discord_gateway_service = None
    container.whatsapp_gateway_service = None
    container.studio_job_recovery_service = studio
    return proactive, world, telegram, studio


def _app_for_role(role: str | None):
    process = ProcessSettings(role=role) if role is not None else ProcessSettings()
    return create_app(AppSettings(database_url="", process=process))


def test_role_api_starts_no_background_but_runs_studio_recovery() -> None:
    app = _app_for_role("api")
    proactive, world, telegram, studio = _install_fakes(app.state.container)

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        # Representative API route is registered (not 404).
        assert client.get("/api/v1/system/version").status_code == 200

    assert proactive.started is False
    assert world.started is False
    assert telegram.started is False
    # Studio executes in-process, so recovery rides with the api role.
    assert studio.called is True


def test_role_api_stops_request_detached_external_chat_turns() -> None:
    app = _app_for_role("api")
    turns = _FakeExternalChatTurns()
    app.state.container.external_chat_turn_service = turns

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200

    assert turns.stop_calls == 1


def test_role_background_serves_only_health_and_starts_schedulers() -> None:
    app = _app_for_role("background")
    proactive, world, telegram, studio = _install_fakes(app.state.container)

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        # No public API surface in the background role.
        assert client.get("/api/v1/system/version").status_code == 404

    assert proactive.started is True
    assert world.started is True
    assert telegram.started is True
    # Studio recovery must NOT run where the executor doesn't live.
    assert studio.called is False
    # Shutdown mirrored what started.
    assert proactive.stopped is True
    assert telegram.stopped is True


def _install_background_loop_fakes(container):
    coordinator = _FakeScheduler()
    worker = _FakeScheduler()
    container.background_shadow_coordinator = coordinator
    container.background_shadow_worker = worker
    return coordinator, worker


def test_role_coordinator_starts_only_coordinator_loop() -> None:
    app = _app_for_role("coordinator")
    proactive, world, telegram, studio = _install_fakes(app.state.container)
    coordinator, worker = _install_background_loop_fakes(app.state.container)

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        # §11 red line: no public API surface on the coordinator role.
        assert client.get("/api/v1/system/version").status_code == 404

    assert coordinator.started is True
    assert coordinator.stopped is True
    # Everything else stays off: no worker, no embedded scheduler, no connectors,
    # no Studio recovery.
    assert worker.started is False
    assert proactive.started is False
    assert world.started is False
    assert telegram.started is False
    assert studio.called is False


def test_role_worker_starts_only_worker_loop() -> None:
    app = _app_for_role("worker")
    proactive, world, telegram, studio = _install_fakes(app.state.container)
    coordinator, worker = _install_background_loop_fakes(app.state.container)

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/api/v1/system/version").status_code == 404

    assert worker.started is True
    assert worker.stopped is True
    assert coordinator.started is False
    assert proactive.started is False
    assert telegram.started is False
    assert studio.called is False


def test_role_connector_starts_only_connectors() -> None:
    app = _app_for_role("connector")
    proactive, world, telegram, studio = _install_fakes(app.state.container)
    coordinator, worker = _install_background_loop_fakes(app.state.container)

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/api/v1/system/version").status_code == 404

    assert telegram.started is True
    assert telegram.stopped is True
    # No queue coordinator/worker, no embedded scheduler, no Studio recovery.
    assert coordinator.started is False
    assert worker.started is False
    assert proactive.started is False
    assert studio.called is False


def test_default_role_preserves_current_behavior() -> None:
    app = _app_for_role(None)  # default ProcessSettings → role "all"
    proactive, world, telegram, studio = _install_fakes(app.state.container)

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/api/v1/system/version").status_code == 200

    # all role: schedulers + connectors + studio recovery all active.
    assert proactive.started is True
    assert world.started is True
    assert telegram.started is True
    assert studio.called is True
