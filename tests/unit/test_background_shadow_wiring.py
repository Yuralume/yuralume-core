"""Shadow runtime container + lifespan wiring (HOSTED_CORE_SCALING §13 Phase 2).

Pins the wiring gate: build the coordinator/worker/queue/lease + scheduler
journal ONLY for a scheduler-owning role on postgres shadow with a DB; an
api-role shadow config builds nothing and warns; shadow off leaves every field
None. The lifespan starts + stops both shadow services in the mirrored position.
"""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from kokoro_link.api.app import create_app
from kokoro_link.bootstrap import app_runtime_settings_seed
from kokoro_link.bootstrap.container import build_container
from kokoro_link.bootstrap.process_settings import ProcessSettings
from kokoro_link.bootstrap.settings import AppSettings

_PG_URL = "postgresql+asyncpg://user:pass@localhost:5432/yuralume_test"


def _neutralise_overlay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        app_runtime_settings_seed,
        "resolve_site_settings_overlay",
        lambda settings: app_runtime_settings_seed.SiteSettingsOverlayResult(
            settings, "env_fallback",
        ),
    )


def _settings(
    role: str,
    shadow: str,
    *,
    backend: str = "embedded",
    database_url: str = _PG_URL,
) -> AppSettings:
    return AppSettings(
        database_url=database_url,
        process=ProcessSettings(
            role=role,
            background_shadow=shadow,
            background_backend=backend,
        ),
    )


# --------------------------------------------------------------------------- #
# container build gate
# --------------------------------------------------------------------------- #


def test_shadow_built_for_background_role_with_db(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _neutralise_overlay(monkeypatch)
    container = build_container(_settings("background", "postgres"))

    assert container.background_shadow_coordinator is not None
    assert container.background_shadow_worker is not None
    assert container.background_job_queue is not None
    assert container.background_coordinator_lease is not None
    # The scheduler got the SA journal + matching bucket wired.
    assert container.proactive_scheduler._tick_journal is not None  # noqa: SLF001
    assert container.proactive_scheduler._bucket_seconds == 300  # noqa: SLF001
    assert container.background_shadow_coordinator._mirror_from_journal is True  # noqa: SLF001


def test_production_postgres_backend_does_not_mirror_journal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _neutralise_overlay(monkeypatch)
    container = build_container(_settings("background", "postgres", backend="postgres"))

    assert container.background_shadow_coordinator is not None
    assert container.background_shadow_coordinator._mirror_from_journal is False  # noqa: SLF001

def test_shadow_built_for_all_role_with_db(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _neutralise_overlay(monkeypatch)
    container = build_container(_settings("all", "postgres"))
    assert container.background_shadow_coordinator is not None
    assert container.background_job_queue is not None


def test_shadow_api_role_builds_queue_port_only(
    monkeypatch: pytest.MonkeyPatch, caplog,
) -> None:
    # M6: an api-serving role on shadow=postgres must build the durable QUEUE +
    # LEASE read ports (the admin diagnostics + internal metrics surfaces live
    # here and were previously permanently blind), but NOT the coordinator/worker
    # tasks or the embedded journal (those co-locate with the singleton
    # scheduler) — and it must NOT warn (api+shadow is a valid config now).
    _neutralise_overlay(monkeypatch)
    with caplog.at_level(logging.WARNING, logger="kokoro_link.bootstrap.container"):
        container = build_container(_settings("api", "postgres"))

    assert container.background_job_queue is not None
    assert container.background_coordinator_lease is not None
    assert container.background_shadow_coordinator is None
    assert container.background_shadow_worker is None
    assert container.proactive_scheduler._tick_journal is None  # noqa: SLF001
    assert not any(
        "starts no schedulers" in rec.message for rec in caplog.records
    )


def test_shadow_warns_only_when_db_missing(
    monkeypatch: pytest.MonkeyPatch, caplog,
) -> None:
    # The remaining warning path (M6): shadow requested but no DB session factory
    # → build nothing and warn. (settings.from_env fail-fasts this combination;
    # constructing AppSettings directly here bypasses that guard on purpose.)
    _neutralise_overlay(monkeypatch)
    with caplog.at_level(logging.WARNING, logger="kokoro_link.bootstrap.container"):
        container = build_container(_settings("api", "postgres", database_url=""))

    assert container.background_job_queue is None
    assert container.background_coordinator_lease is None
    assert any(
        "no database session factory" in rec.message for rec in caplog.records
    )


def test_coordinator_role_builds_coordinator_not_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # §2.1 dedicated coordinator (postgres backend): builds the coordinator loop
    # + queue/lease read ports, but NO worker and NO execution runner. It obeys
    # the execution-mode barrier via a wired runtime_ownership.
    _neutralise_overlay(monkeypatch)
    container = build_container(
        _settings("coordinator", "", backend="postgres"),
    )
    assert container.background_shadow_coordinator is not None
    assert container.background_shadow_worker is None
    assert container.background_job_queue is not None
    assert container.background_coordinator_lease is not None
    assert container.runtime_ownership is not None
    # A distributed coordinator never mirrors the embedded journal.
    assert container.background_shadow_coordinator._mirror_from_journal is False  # noqa: SLF001


def test_worker_role_builds_worker_and_execution_not_coordinator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # §2.1 dedicated worker (postgres backend): builds the worker claim/execute
    # loop upgraded to mode-aware execution, but NO coordinator loop.
    _neutralise_overlay(monkeypatch)
    container = build_container(_settings("worker", "", backend="postgres"))
    assert container.background_shadow_coordinator is None
    worker = container.background_shadow_worker
    assert worker is not None
    assert worker._execution_runner is not None  # noqa: SLF001
    assert worker._runtime_ownership is not None  # noqa: SLF001
    assert container.background_job_queue is not None
    assert container.runtime_ownership is not None


def test_connector_role_builds_no_coordinator_or_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # §2.1 dedicated connector: no queue coordinator/worker at all. It runs on the
    # default embedded backend (per-account TTL lease, not the queue), so the
    # durable read ports are absent too.
    _neutralise_overlay(monkeypatch)
    container = build_container(
        _settings("connector", "", backend="embedded", database_url=_PG_URL),
    )
    assert container.background_shadow_coordinator is None
    assert container.background_shadow_worker is None
    assert container.background_job_queue is None


def test_shadow_off_leaves_all_fields_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _neutralise_overlay(monkeypatch)
    container = build_container(_settings("background", "", database_url=_PG_URL))
    assert container.background_shadow_coordinator is None
    assert container.background_shadow_worker is None
    assert container.background_job_queue is None
    assert container.background_coordinator_lease is None
    assert container.proactive_scheduler._tick_journal is None  # noqa: SLF001


# --------------------------------------------------------------------------- #
# lifespan start/stop gating
# --------------------------------------------------------------------------- #


class _FakeService:
    def __init__(self) -> None:
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    @property
    def is_running(self) -> bool:
        return self.started and not self.stopped


def _install_scheduler_fakes(container):
    for name in (
        "proactive_scheduler",
        "world_event_scheduler",
        "telegram_polling_service",
        "discord_gateway_service",
        "whatsapp_gateway_service",
    ):
        setattr(container, name, None)


def test_lifespan_starts_and_stops_shadow_services() -> None:
    # In-memory build (no DB) leaves shadow fields None; inject fakes so the
    # lifespan gating can be exercised without a database.
    app = create_app(
        AppSettings(
            database_url="",
            process=ProcessSettings(role="background", background_shadow=""),
        ),
    )
    container = app.state.container
    _install_scheduler_fakes(container)
    coordinator = _FakeService()
    worker = _FakeService()
    container.background_shadow_coordinator = coordinator
    container.background_shadow_worker = worker

    with TestClient(app):
        assert coordinator.started is True
        assert worker.started is True

    assert coordinator.stopped is True
    assert worker.stopped is True


def test_lifespan_all_role_without_shadow_starts_nothing() -> None:
    app = create_app(AppSettings(database_url=""))
    container = app.state.container
    # Shadow off → fields stay None and the lifespan never touches them.
    assert container.background_shadow_coordinator is None
    assert container.background_shadow_worker is None
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
