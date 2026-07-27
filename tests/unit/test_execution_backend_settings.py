"""Execution backend unlock + wiring (HOSTED_CORE_SCALING §13 Phase 3-C).

``YURALUME_BACKGROUND_BACKEND=postgres`` is now a valid backend (P3-C unlock):
it requires a database (fail-fast, same style as shadow), and when wired with a
DB it upgrades the shadow worker to mode-aware execution, turns on schedule
staggering, and enables durable warmup enqueue. The embedded default stays the
byte-identical self-host red line: no execution, no staggering, no warmup.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from kokoro_link.bootstrap.process_settings import ProcessSettings
from kokoro_link.bootstrap.settings import AppSettings


def _base_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KOKORO_DEPLOYMENT_MODE", "test")
    monkeypatch.setenv("KOKORO_STORAGE_PROVIDER", "memory")
    for key in (
        "YURALUME_PROCESS_ROLE",
        "YURALUME_BACKGROUND_BACKEND",
        "YURALUME_BACKGROUND_SHADOW",
        "DATABASE_URL",
        "KOKORO_DATABASE_URL",
    ):
        monkeypatch.delenv(key, raising=False)


def test_backend_postgres_without_database_fails_fast(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv("YURALUME_BACKGROUND_BACKEND", "postgres")
    with pytest.raises(ValueError) as excinfo:
        AppSettings.from_env(project_root=tmp_path)
    message = str(excinfo.value)
    assert "YURALUME_BACKGROUND_BACKEND" in message
    assert "database" in message.lower()


def test_backend_postgres_with_database_ok(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv("YURALUME_BACKGROUND_BACKEND", "postgres")
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql+asyncpg://u:p@localhost:5432/db",
    )
    settings = AppSettings.from_env(project_root=tmp_path)
    assert settings.process.background_backend == "postgres"


def test_distributed_roles_require_postgres_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # §2.1 unlock: coordinator/worker are now valid roles but demand the
    # postgres backend (they enqueue into / claim from the durable queue).
    from kokoro_link.bootstrap.process_settings import load_process_settings

    _base_env(monkeypatch)
    for role in ("coordinator", "worker"):
        monkeypatch.setenv("YURALUME_PROCESS_ROLE", role)
        monkeypatch.delenv("YURALUME_BACKGROUND_BACKEND", raising=False)
        with pytest.raises(ValueError, match="distributed queue backend"):
            load_process_settings()


def test_connector_role_accepted_on_default_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kokoro_link.bootstrap.process_settings import load_process_settings

    _base_env(monkeypatch)
    monkeypatch.setenv("YURALUME_PROCESS_ROLE", "connector")
    assert load_process_settings().role == "connector"


def _postgres_settings() -> AppSettings:
    base = AppSettings(database_url="postgresql+asyncpg://u:p@localhost:5432/db")
    return replace(
        base,
        process=ProcessSettings(role="all", background_backend="postgres"),
    )


def test_backend_postgres_container_wires_execution() -> None:
    from kokoro_link.bootstrap.container import build_container

    container = build_container(_postgres_settings())
    worker = container.background_shadow_worker
    assert worker is not None
    # Upgraded from dry-run to mode-aware execution.
    assert worker._execution_runner is not None  # noqa: SLF001
    assert worker._runtime_ownership is not None  # noqa: SLF001
    assert container.background_shadow_coordinator is not None
    assert container.background_job_queue is not None
    assert container.runtime_ownership is not None
    # Staggering + durable warmup enabled on the execution backend.
    assert container.schedule_service._staggering_enabled is True  # noqa: SLF001
    assert (
        container.character_service._warmup_enqueue_enabled is True  # noqa: SLF001
    )


def test_backend_embedded_container_no_execution() -> None:
    # Self-host red line: embedded backend leaves the worker pure dry-run, no
    # ownership, no staggering, no warmup.
    from kokoro_link.bootstrap.container import build_container

    container = build_container(AppSettings(database_url=""))
    assert container.background_shadow_worker is None
    assert container.runtime_ownership is None
    assert container.schedule_service._staggering_enabled is False  # noqa: SLF001
    assert (
        container.character_service._warmup_enqueue_enabled is False  # noqa: SLF001
    )
