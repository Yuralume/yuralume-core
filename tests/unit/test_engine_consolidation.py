"""Composition-root engine consolidation (HOSTED_CORE_SCALING §9.1 / §13 P1).

W2 collapses the historical ~10 per-site ``build_async_engine`` calls in
``build_container`` into ONE shared async engine + ONE session factory per
process, stores the engine on the container, disposes it once in the app
lifespan, and lets the per-process pool budget be tuned via
``YURALUME_DB_POOL_SIZE`` / ``YURALUME_DB_MAX_OVERFLOW``.

These tests pin all four guarantees:
* every SA repository binds to the single ``container.db_engine`` object;
* the pool-budget envs round-trip and fail-fast on garbage;
* the in-memory path leaves ``db_engine`` ``None`` (no dispose to run);
* the app lifespan awaits ``engine.dispose()`` on shutdown.

The engines are lazy (``create_async_engine`` opens no socket), so the
postgres-URL build needs no live server — but the container-build
runtime-settings overlay *does* try to read the DB, so we neutralise that
one fail-soft seed for the build tests.
"""

from __future__ import annotations

import dataclasses

import pytest
from fastapi.testclient import TestClient

from kokoro_link.api.app import create_app
from kokoro_link.bootstrap import app_runtime_settings_seed
from kokoro_link.bootstrap.container import build_container
from kokoro_link.bootstrap.settings import AppSettings

_PG_URL = "postgresql+asyncpg://user:pass@localhost:5432/yuralume_test"


def _neutralise_overlay(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop the container-build site-settings overlay from touching the DB.

    ``resolve_site_settings_overlay`` is fail-soft (a connection error just
    logs + returns env settings), but an unreachable host can stall the
    build on a TCP timeout. Patch it to an identity so the build is pure
    wiring — exactly the DB-free construction the engines' laziness allows.

    Patched by its real name rather than the ``overlay_site_settings_from_db``
    shim: ``build_container`` imports the former, so patching the shim would
    silently neutralise nothing and quietly reintroduce the stall.
    """
    monkeypatch.setattr(
        app_runtime_settings_seed,
        "resolve_site_settings_overlay",
        lambda settings: app_runtime_settings_seed.SiteSettingsOverlayResult(
            settings, "env_fallback",
        ),
    )


def _bound_sa_repo_engines(container) -> list[tuple[str, object]]:
    """(field_name, bound engine) for every container field that is an SA repo.

    SA repositories uniformly hold their ``sessionmaker`` on
    ``_session_factory``; the bound engine is ``sessionmaker.kw['bind']``.
    In-memory repos have no such attribute and are skipped, so this both
    enumerates the DB-backed surface and reads back its engine.
    """
    bound: list[tuple[str, object]] = []
    for f in dataclasses.fields(container):
        value = getattr(container, f.name)
        factory = getattr(value, "_session_factory", None)
        if factory is None:
            continue
        bound.append((f.name, factory.kw.get("bind")))
    return bound


def test_all_sa_repositories_share_one_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _neutralise_overlay(monkeypatch)

    container = build_container(AppSettings(database_url=_PG_URL))

    assert container.db_engine is not None
    bound = _bound_sa_repo_engines(container)
    # Guard against a future refactor that silently drops every SA repo to
    # in-memory (which would make the "all share one engine" claim vacuous).
    assert len(bound) >= 20, f"expected many SA repos, got {len(bound)}: {bound}"
    offenders = [
        name for name, engine in bound if engine is not container.db_engine
    ]
    assert not offenders, (
        "these SA repos are not bound to the shared container.db_engine: "
        f"{offenders}"
    )


def test_pool_budget_flows_from_settings_into_the_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _neutralise_overlay(monkeypatch)

    settings = AppSettings(
        database_url=_PG_URL,
        db_pool_size=7,
        db_max_overflow=3,
    )
    container = build_container(settings)

    pool = container.db_engine.sync_engine.pool
    # QueuePool exposes the configured size + overflow ceiling.
    assert pool.size() == 7
    assert pool._max_overflow == 3


def test_in_memory_build_has_no_engine_and_no_sa_repos() -> None:
    container = build_container(AppSettings(database_url=""))

    assert container.db_engine is None
    assert _bound_sa_repo_engines(container) == []


class _RecordingEngine:
    """Stand-in for an AsyncEngine that records a single awaited dispose()."""

    def __init__(self) -> None:
        self.dispose_calls = 0

    async def dispose(self) -> None:
        self.dispose_calls += 1


def test_lifespan_disposes_the_shared_engine_on_shutdown() -> None:
    app = create_app(AppSettings(database_url=""))
    engine = _RecordingEngine()
    # In-memory build leaves db_engine None; inject a recording stub so the
    # lifespan finally has something to dispose (the real engine has the
    # same async dispose() seam).
    app.state.container.db_engine = engine

    with TestClient(app):
        assert engine.dispose_calls == 0  # not disposed until shutdown

    assert engine.dispose_calls == 1


def test_lifespan_shutdown_survives_a_dispose_failure() -> None:
    class _ExplodingEngine:
        async def dispose(self) -> None:
            raise RuntimeError("pool already closed")

    app = create_app(AppSettings(database_url=""))
    app.state.container.db_engine = _ExplodingEngine()

    # Fail-soft: a dispose error must not propagate out of shutdown.
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200


# --- pool-budget env parsing (AppSettings.from_env) ------------------------


def _env_for_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Minimal env so ``from_env`` reaches the pool-budget parse cleanly.

    ``memory`` storage + non-container mode skips the http-storage
    validation that would otherwise raise first.
    """
    monkeypatch.setenv("KOKORO_DEPLOYMENT_MODE", "test")
    monkeypatch.setenv("KOKORO_STORAGE_PROVIDER", "memory")


def test_pool_envs_round_trip(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    _env_for_settings(monkeypatch)
    monkeypatch.setenv("YURALUME_DB_POOL_SIZE", "12")
    monkeypatch.setenv("YURALUME_DB_MAX_OVERFLOW", "40")

    settings = AppSettings.from_env(project_root=tmp_path)

    assert settings.db_pool_size == 12
    assert settings.db_max_overflow == 40


def test_pool_envs_absent_use_historical_defaults(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    _env_for_settings(monkeypatch)
    monkeypatch.delenv("YURALUME_DB_POOL_SIZE", raising=False)
    monkeypatch.delenv("YURALUME_DB_MAX_OVERFLOW", raising=False)

    settings = AppSettings.from_env(project_root=tmp_path)

    assert settings.db_pool_size == 5
    assert settings.db_max_overflow == 10


@pytest.mark.parametrize(
    "env_name",
    ["YURALUME_DB_POOL_SIZE", "YURALUME_DB_MAX_OVERFLOW"],
)
@pytest.mark.parametrize("bad_value", ["ten", "", "  ", "3.5"])
def test_pool_envs_reject_non_integer(
    monkeypatch: pytest.MonkeyPatch, tmp_path, env_name: str, bad_value: str,
) -> None:
    _env_for_settings(monkeypatch)
    if bad_value.strip() == "":
        # Blank / whitespace is treated as "absent" → default, never raises.
        monkeypatch.setenv(env_name, bad_value)
        settings = AppSettings.from_env(project_root=tmp_path)
        assert settings.db_pool_size >= 1 and settings.db_max_overflow >= 1
        return
    monkeypatch.setenv(env_name, bad_value)
    with pytest.raises(ValueError, match=env_name):
        AppSettings.from_env(project_root=tmp_path)


# pool_size is a strictly-positive knob: 0 and negatives both fail-fast.
@pytest.mark.parametrize("bad_value", ["0", "-1", "-20"])
def test_pool_size_rejects_below_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path, bad_value: str,
) -> None:
    _env_for_settings(monkeypatch)
    monkeypatch.setenv("YURALUME_DB_POOL_SIZE", bad_value)
    with pytest.raises(ValueError, match="YURALUME_DB_POOL_SIZE"):
        AppSettings.from_env(project_root=tmp_path)


# max_overflow is non-negative: 0 is a legal SQLAlchemy value (fixed X/0 pool,
# §9.1 budget table), so only NEGATIVE values fail-fast.
@pytest.mark.parametrize("bad_value", ["-1", "-20"])
def test_max_overflow_rejects_negative(
    monkeypatch: pytest.MonkeyPatch, tmp_path, bad_value: str,
) -> None:
    _env_for_settings(monkeypatch)
    monkeypatch.setenv("YURALUME_DB_MAX_OVERFLOW", bad_value)
    with pytest.raises(ValueError, match="YURALUME_DB_MAX_OVERFLOW"):
        AppSettings.from_env(project_root=tmp_path)


def test_max_overflow_accepts_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    # SQLAlchemy max_overflow=0 is legal — a fixed pool_size/0 pool with no
    # burst connections. Must round-trip, not be rejected like pool_size=0.
    _env_for_settings(monkeypatch)
    monkeypatch.setenv("YURALUME_DB_MAX_OVERFLOW", "0")

    settings = AppSettings.from_env(project_root=tmp_path)

    assert settings.db_max_overflow == 0


def test_build_async_engine_applies_pool_kwargs_only_for_asyncpg() -> None:
    from kokoro_link.infrastructure.persistence.engine import build_async_engine

    pooled = build_async_engine(_PG_URL, pool_size=9, max_overflow=2)
    assert pooled.sync_engine.pool.size() == 9
    assert pooled.sync_engine.pool._max_overflow == 2


def test_zero_max_overflow_flows_into_the_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The X/0 fixed-pool budget must survive all the way into the live engine.
    _neutralise_overlay(monkeypatch)

    container = build_container(
        AppSettings(database_url=_PG_URL, db_pool_size=4, db_max_overflow=0),
    )

    pool = container.db_engine.sync_engine.pool
    assert pool.size() == 4
    assert pool._max_overflow == 0
