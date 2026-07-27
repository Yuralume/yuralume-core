"""Phase 1 process-role contracts (HOSTED_CORE_SCALING_ARCHITECTURE_PLAN §2.1).

Covers the component matrix per role and the ``ProcessSettings`` env loader:
zero-config default (all + embedded), env round-trip, and fail-fast on invalid /
reserved / not-yet-implemented values.
"""

from __future__ import annotations

import pytest

from kokoro_link.bootstrap.process_roles import ComponentMatrix, matrix_for_role
from kokoro_link.bootstrap.process_settings import (
    ProcessSettings,
    load_process_settings,
)

_PROCESS_ENV_KEYS = (
    "YURALUME_PROCESS_ROLE",
    "YURALUME_BACKGROUND_BACKEND",
    # Retired in Phase 4 — cleared so a leaked env can't trip the fail-fast in
    # unrelated tests, and asserted-on in the dedicated fail-fast test below.
    "YURALUME_REALTIME_RELAY_URL",
    "YURALUME_REALTIME_RELAY_INTERNAL_TOKEN",
    "YURALUME_METRICS_INTERNAL_TOKEN",
    "YURALUME_BACKGROUND_SHADOW",
    "YURALUME_REALTIME_BACKEND",
    "YURALUME_REALTIME_POLL_INTERVAL",
    "YURALUME_RUNTIME_CONFIG_POLL_INTERVAL",
)


def _clear_process_env(monkeypatch) -> None:
    for key in _PROCESS_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


# --------------------------------------------------------------------------- #
# Component matrix
# --------------------------------------------------------------------------- #


def test_matrix_all_starts_everything_without_relay() -> None:
    matrix = matrix_for_role("all")
    assert matrix == ComponentMatrix(
        serve_api_routes=True,
        serve_cloud_internal_routes=True,
        serve_metrics_route=True,
        start_schedulers=True,
        start_connectors=True,
        run_studio_recovery=True,
        run_background_coordinator=True,
        run_background_worker=True,
        enable_realtime_outbox_writer=False,
    )


def test_matrix_api_serves_routes_and_studio_recovery_but_no_schedulers() -> None:
    matrix = matrix_for_role("api")
    assert matrix == ComponentMatrix(
        serve_api_routes=True,
        serve_cloud_internal_routes=True,
        serve_metrics_route=True,
        start_schedulers=False,
        start_connectors=False,
        run_studio_recovery=True,
        run_background_coordinator=False,
        run_background_worker=False,
        enable_realtime_outbox_writer=False,
    )


def test_matrix_background_runs_headless_schedulers_and_relay() -> None:
    matrix = matrix_for_role("background")
    assert matrix == ComponentMatrix(
        serve_api_routes=False,
        serve_cloud_internal_routes=False,
        serve_metrics_route=True,
        start_schedulers=True,
        start_connectors=True,
        run_studio_recovery=False,
        run_background_coordinator=True,
        run_background_worker=True,
        enable_realtime_outbox_writer=True,
    )


def test_matrix_coordinator_runs_only_coordinator_loop() -> None:
    matrix = matrix_for_role("coordinator")
    assert matrix == ComponentMatrix(
        serve_api_routes=False,
        serve_cloud_internal_routes=False,
        serve_metrics_route=True,
        start_schedulers=False,
        start_connectors=False,
        run_studio_recovery=False,
        run_background_coordinator=True,
        run_background_worker=False,
        enable_realtime_outbox_writer=False,
    )


def test_matrix_worker_runs_only_worker_loop_and_outbox_writer() -> None:
    matrix = matrix_for_role("worker")
    assert matrix == ComponentMatrix(
        serve_api_routes=False,
        serve_cloud_internal_routes=False,
        serve_metrics_route=True,
        start_schedulers=False,
        start_connectors=False,
        run_studio_recovery=False,
        run_background_coordinator=False,
        run_background_worker=True,
        enable_realtime_outbox_writer=True,
    )


def test_matrix_connector_runs_only_connectors() -> None:
    matrix = matrix_for_role("connector")
    assert matrix == ComponentMatrix(
        serve_api_routes=False,
        serve_cloud_internal_routes=False,
        serve_metrics_route=True,
        start_schedulers=False,
        start_connectors=True,
        run_studio_recovery=False,
        run_background_coordinator=False,
        run_background_worker=False,
        enable_realtime_outbox_writer=False,
    )


def test_matrix_security_red_line_headless_roles_serve_no_api() -> None:
    # §11 red line: none of the headless roles register public API / cloud
    # internal routes; the only surface they expose is the loopback metrics.
    for role in ("background", "coordinator", "worker", "connector"):
        matrix = matrix_for_role(role)
        assert matrix.serve_api_routes is False
        assert matrix.serve_cloud_internal_routes is False
        assert matrix.serve_metrics_route is True


def test_matrix_studio_recovery_only_where_api_routes_served() -> None:
    # Studio executes inside the API process; recovery must ride with it.
    for role in ("all", "api", "background", "coordinator", "worker", "connector"):
        matrix = matrix_for_role(role)
        assert matrix.run_studio_recovery == matrix.serve_api_routes


def test_matrix_outbox_writer_only_on_tick_executing_headless_roles() -> None:
    # The outbox writer wraps the buses precisely where ticks are executed but no
    # SSE is served: background (embedded exec) + worker (distributed exec).
    assert matrix_for_role("background").enable_realtime_outbox_writer is True
    assert matrix_for_role("worker").enable_realtime_outbox_writer is True
    # A pure coordinator only enqueues (no proactive/feed publish) → no writer.
    assert matrix_for_role("coordinator").enable_realtime_outbox_writer is False
    for role in ("all", "api", "connector"):
        assert matrix_for_role(role).enable_realtime_outbox_writer is False


def test_matrix_unknown_role_raises() -> None:
    with pytest.raises(ValueError, match="component matrix"):
        matrix_for_role("nope")


# --------------------------------------------------------------------------- #
# ProcessSettings env loader
# --------------------------------------------------------------------------- #


def test_process_settings_default_is_all_embedded_with_no_env(monkeypatch) -> None:
    _clear_process_env(monkeypatch)
    settings = load_process_settings()
    assert settings == ProcessSettings(
        role="all",
        background_backend="embedded",
        metrics_internal_token="",
    )


def test_process_settings_dataclass_default_matches_self_host() -> None:
    # The pure dataclass default (constructed directly by tests) must also be
    # the self-host red line, independent of env.
    assert ProcessSettings() == ProcessSettings(
        role="all", background_backend="embedded",
    )


def test_process_settings_env_round_trip(monkeypatch) -> None:
    _clear_process_env(monkeypatch)
    monkeypatch.setenv("YURALUME_PROCESS_ROLE", "background")
    monkeypatch.setenv("YURALUME_BACKGROUND_BACKEND", "embedded")
    monkeypatch.setenv(
        "YURALUME_METRICS_INTERNAL_TOKEN", "metrics-secret",
    )

    settings = load_process_settings()

    assert settings.role == "background"
    assert settings.background_backend == "embedded"
    assert settings.metrics_internal_token == "metrics-secret"


# --------------------------------------------------------------------------- #
# Phase 4 realtime backend + dispatcher poll cadence (§7.1)
# --------------------------------------------------------------------------- #


def test_process_settings_realtime_backend_defaults_to_memory(monkeypatch) -> None:
    _clear_process_env(monkeypatch)
    settings = load_process_settings()
    assert settings.realtime_backend == "memory"
    # The dispatcher poll cadence has a sane default even off the postgres path.
    assert settings.realtime_poll_interval == 2.0


def test_process_settings_realtime_backend_postgres_round_trip(monkeypatch) -> None:
    _clear_process_env(monkeypatch)
    monkeypatch.setenv("YURALUME_REALTIME_BACKEND", "postgres")
    assert load_process_settings().realtime_backend == "postgres"


def test_process_settings_realtime_backend_invalid_fail_fast(monkeypatch) -> None:
    _clear_process_env(monkeypatch)
    monkeypatch.setenv("YURALUME_REALTIME_BACKEND", "redis")
    with pytest.raises(ValueError) as excinfo:
        load_process_settings()
    message = str(excinfo.value)
    assert "YURALUME_REALTIME_BACKEND" in message
    assert "postgres" in message


def test_process_settings_poll_interval_env_override(monkeypatch) -> None:
    _clear_process_env(monkeypatch)
    monkeypatch.setenv("YURALUME_REALTIME_POLL_INTERVAL", "0.5")
    assert load_process_settings().realtime_poll_interval == 0.5


@pytest.mark.parametrize("bad", ["abc", "0", "-1"])
def test_process_settings_poll_interval_invalid_fail_fast(monkeypatch, bad) -> None:
    _clear_process_env(monkeypatch)
    monkeypatch.setenv("YURALUME_REALTIME_POLL_INTERVAL", bad)
    with pytest.raises(ValueError, match="YURALUME_REALTIME_POLL_INTERVAL"):
        load_process_settings()


def test_process_settings_runtime_config_poll_interval_env_override(
    monkeypatch,
) -> None:
    _clear_process_env(monkeypatch)
    assert load_process_settings().runtime_config_poll_interval == 45.0
    monkeypatch.setenv("YURALUME_RUNTIME_CONFIG_POLL_INTERVAL", "10")
    assert load_process_settings().runtime_config_poll_interval == 10.0


@pytest.mark.parametrize(
    "env_key",
    ["YURALUME_REALTIME_POLL_INTERVAL", "YURALUME_RUNTIME_CONFIG_POLL_INTERVAL"],
)
@pytest.mark.parametrize("bad", ["nan", "NaN", "inf", "Infinity", "-inf"])
def test_process_settings_poll_interval_rejects_non_finite(
    monkeypatch, env_key, bad,
) -> None:
    """``float()`` happily parses these and ``NaN <= 0`` is False, so the
    positivity check alone let them through: NaN would make every
    ``wait_for(timeout=nan)`` raise and Infinity would park the fallback poll
    forever, silently disabling the safety net the knob configures."""
    _clear_process_env(monkeypatch)
    monkeypatch.setenv(env_key, bad)
    with pytest.raises(ValueError, match=env_key):
        load_process_settings()


# --------------------------------------------------------------------------- #
# Retired §7.0 relay env → fail-fast (Phase 4)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "env_key",
    ["YURALUME_REALTIME_RELAY_URL", "YURALUME_REALTIME_RELAY_INTERNAL_TOKEN"],
)
def test_process_settings_retired_relay_env_fail_fast(monkeypatch, env_key) -> None:
    _clear_process_env(monkeypatch)
    monkeypatch.setenv(env_key, "http://app-api:8002")
    with pytest.raises(ValueError) as excinfo:
        load_process_settings()
    message = str(excinfo.value)
    assert env_key in message
    # Points the operator at the Phase 4 replacement.
    assert "YURALUME_REALTIME_BACKEND=postgres" in message


def test_process_settings_role_is_case_insensitive(monkeypatch) -> None:
    _clear_process_env(monkeypatch)
    monkeypatch.setenv("YURALUME_PROCESS_ROLE", "API")
    assert load_process_settings().role == "api"


@pytest.mark.parametrize("role", ["coordinator", "worker"])
def test_process_settings_distributed_roles_require_postgres_backend(
    monkeypatch, role,
) -> None:
    # §2.1 unlock: coordinator/worker only make sense on the distributed queue —
    # they fail-fast under the (default) embedded backend, naming the override.
    _clear_process_env(monkeypatch)
    monkeypatch.setenv("YURALUME_PROCESS_ROLE", role)
    with pytest.raises(ValueError) as excinfo:
        load_process_settings()
    message = str(excinfo.value)
    assert "YURALUME_PROCESS_ROLE" in message
    assert "YURALUME_BACKGROUND_BACKEND=postgres" in message


@pytest.mark.parametrize("role", ["coordinator", "worker"])
def test_process_settings_distributed_roles_accepted_on_postgres(
    monkeypatch, role,
) -> None:
    _clear_process_env(monkeypatch)
    monkeypatch.setenv("YURALUME_PROCESS_ROLE", role)
    monkeypatch.setenv("YURALUME_BACKGROUND_BACKEND", "postgres")
    settings = load_process_settings()
    assert settings.role == role
    assert settings.background_backend == "postgres"


def test_process_settings_connector_role_needs_no_backend(monkeypatch) -> None:
    # Connectors lease per-account TTL rows, not queue jobs — a connector process
    # runs on the default embedded backend with no distributed queue at all.
    _clear_process_env(monkeypatch)
    monkeypatch.setenv("YURALUME_PROCESS_ROLE", "connector")
    settings = load_process_settings()
    assert settings.role == "connector"
    assert settings.background_backend == "embedded"


def test_process_settings_unknown_role_names_allowed_values(monkeypatch) -> None:
    _clear_process_env(monkeypatch)
    monkeypatch.setenv("YURALUME_PROCESS_ROLE", "banana")
    with pytest.raises(ValueError) as excinfo:
        load_process_settings()
    message = str(excinfo.value)
    assert "YURALUME_PROCESS_ROLE" in message
    # Names the full allowed set so the operator can self-correct.
    for role in ("all", "api", "background", "coordinator", "worker", "connector"):
        assert role in message


def test_process_settings_postgres_backend_now_accepted(monkeypatch) -> None:
    # P3-C unlock: the postgres execution backend is now a valid process backend.
    # The database requirement is enforced at the AppSettings layer (settings.py),
    # not here — load_process_settings only validates the enum.
    _clear_process_env(monkeypatch)
    monkeypatch.setenv("YURALUME_BACKGROUND_BACKEND", "postgres")
    settings = load_process_settings()
    assert settings.background_backend == "postgres"


def test_process_settings_unknown_backend_names_allowed_values(monkeypatch) -> None:
    _clear_process_env(monkeypatch)
    monkeypatch.setenv("YURALUME_BACKGROUND_BACKEND", "redis")
    with pytest.raises(ValueError) as excinfo:
        load_process_settings()
    message = str(excinfo.value)
    assert "YURALUME_BACKGROUND_BACKEND" in message
    assert "embedded" in message


# --------------------------------------------------------------------------- #
# P2-B shadow toggle (HOSTED_CORE_SCALING §13 Phase 2)
# --------------------------------------------------------------------------- #


def test_process_settings_shadow_default_off(monkeypatch) -> None:
    _clear_process_env(monkeypatch)
    assert load_process_settings().background_shadow == ""


def test_process_settings_dataclass_default_shadow_off() -> None:
    assert ProcessSettings().background_shadow == ""


def test_process_settings_shadow_postgres_round_trip(monkeypatch) -> None:
    _clear_process_env(monkeypatch)
    monkeypatch.setenv("YURALUME_BACKGROUND_SHADOW", "postgres")
    assert load_process_settings().background_shadow == "postgres"


def test_process_settings_shadow_case_insensitive(monkeypatch) -> None:
    _clear_process_env(monkeypatch)
    monkeypatch.setenv("YURALUME_BACKGROUND_SHADOW", "POSTGRES")
    assert load_process_settings().background_shadow == "postgres"


def test_process_settings_shadow_invalid_fail_fast(monkeypatch) -> None:
    _clear_process_env(monkeypatch)
    monkeypatch.setenv("YURALUME_BACKGROUND_SHADOW", "redis")
    with pytest.raises(ValueError) as excinfo:
        load_process_settings()
    message = str(excinfo.value)
    assert "YURALUME_BACKGROUND_SHADOW" in message
    assert "postgres" in message

def test_only_coordinator_omits_cloud_provider_credentials() -> None:
    assert matrix_for_role("coordinator").requires_cloud_provider_credentials is False
    for role in ("all", "api", "background", "worker", "connector"):
        assert matrix_for_role(role).requires_cloud_provider_credentials is True
