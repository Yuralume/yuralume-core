"""AppSettings shadow gate (HOSTED_CORE_SCALING §13 Phase 2).

The ``YURALUME_BACKGROUND_SHADOW`` toggle only makes sense with the durable
PostgreSQL queue, so ``postgres`` without a database fails fast at boot. The
default (unset → "") is the self-host red line: zero-config, byte-identical.
"""

from __future__ import annotations

import pytest

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


def test_shadow_postgres_without_database_fails_fast(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv("YURALUME_BACKGROUND_SHADOW", "postgres")
    with pytest.raises(ValueError) as excinfo:
        AppSettings.from_env(project_root=tmp_path)
    message = str(excinfo.value)
    assert "YURALUME_BACKGROUND_SHADOW" in message
    assert "database" in message.lower()


def test_shadow_postgres_with_database_ok(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv("YURALUME_BACKGROUND_SHADOW", "postgres")
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql+asyncpg://u:p@localhost:5432/db",
    )
    settings = AppSettings.from_env(project_root=tmp_path)
    assert settings.process.background_shadow == "postgres"


def test_shadow_unset_is_zero_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    # Self-host red line: unset shadow with no DB boots exactly as before.
    _base_env(monkeypatch)
    settings = AppSettings.from_env(project_root=tmp_path)
    assert settings.process.background_shadow == ""
    assert settings.process.role == "all"
    assert settings.process.background_backend == "embedded"


def test_shadow_off_container_fields_none() -> None:
    # A container built without shadow leaves every shadow field None (no tasks,
    # no queue port, no journal) — the byte-identical self-host guarantee.
    from kokoro_link.bootstrap.container import build_container

    container = build_container(AppSettings(database_url=""))
    assert container.background_shadow_coordinator is None
    assert container.background_shadow_worker is None
    assert container.background_job_queue is None
    assert container.background_coordinator_lease is None
    # The scheduler got no journal wired.
    assert container.proactive_scheduler is not None
    assert container.proactive_scheduler._tick_journal is None  # noqa: SLF001
    assert container.proactive_scheduler._bucket_seconds is None  # noqa: SLF001
