"""Process-role settings group.

A single Core image runs in one of several *process roles*. Self-host keeps
the zero-config ``all`` role (embedded schedulers + connectors, no external
dependency); Hosted deployments split the image into ``api`` and
``background`` processes via env-only overrides.

Kept in its own module because ``bootstrap/settings.py`` is already large and
this group has its own fail-fast validation surface. ``AppSettings`` wires the
loader in; the dataclass itself stays pure (default ``all`` + ``embedded``) so
tests can construct it directly without going through env validation.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass

# Env keys — YURALUME_ prefix only, no KOKORO_ fallback (convention of the
# newest settings groups).
_ROLE_ENV = "YURALUME_PROCESS_ROLE"
_BACKEND_ENV = "YURALUME_BACKGROUND_BACKEND"
# Phase 4 (§7.1) retired the §7.0 HTTP relay in favour of the PostgreSQL outbox.
# These env keys are kept ONLY to fail-fast a stale Hosted config that still
# sets them, pointing the operator at the replacement backend.
_RELAY_URL_ENV = "YURALUME_REALTIME_RELAY_URL"
_RELAY_TOKEN_ENV = "YURALUME_REALTIME_RELAY_INTERNAL_TOKEN"
_METRICS_TOKEN_ENV = "YURALUME_METRICS_INTERNAL_TOKEN"
_SHADOW_ENV = "YURALUME_BACKGROUND_SHADOW"
_REALTIME_BACKEND_ENV = "YURALUME_REALTIME_BACKEND"
_REALTIME_POLL_INTERVAL_ENV = "YURALUME_REALTIME_POLL_INTERVAL"
_RUNTIME_CONFIG_POLL_INTERVAL_ENV = "YURALUME_RUNTIME_CONFIG_POLL_INTERVAL"

# Fallback poll cadence (seconds) for the api-side outbox dispatcher — a safety
# net behind LISTEN/NOTIFY, so a dropped notification costs latency, never
# delivery. Mirrors the dispatcher module default; overridable via env.
_DEFAULT_REALTIME_POLL_INTERVAL = 2.0

# Fallback poll cadence (seconds) for the runtime-config refresher. Much slower
# than the realtime one: config changes are operator-driven (rare), and each
# tick costs one aggregate read per process, so there is nothing to gain from
# polling hard behind LISTEN.
_DEFAULT_RUNTIME_CONFIG_POLL_INTERVAL = 45.0

# Phase 4 realtime outbox (§7.1). ``memory`` = self-host red line: the historical
# in-process ProactiveEventBus/FeedEventBus, no PostgreSQL outbox, no new env.
# ``postgres`` opts a distributed deployment into the transactional outbox +
# NOTIFY relay. Parsed here; wiring is a later task.
_IMPLEMENTED_REALTIME_BACKENDS = ("memory", "postgres")

# P2-B shadow runtime toggle. ``""`` = off (self-host red line: zero tasks, no
# journal, no metrics series). ``"postgres"`` turns on the coordinator +
# dry-run worker + embedded tick journal against the shared PostgreSQL queue.
_IMPLEMENTED_SHADOWS = ("", "postgres")

# Every implemented process role (§2.1). ``coordinator``/``worker``/``connector``
# unlocked in §2.1 "dedicated process roles": each starts one slice of the
# distributed execution machinery in its own process (see ``process_roles.py``).
# ``coordinator``/``worker`` are meaningful ONLY on the distributed queue backend
# (they enqueue into / claim from the durable PostgreSQL queue), so they fail-fast
# under the embedded backend. ``connector`` uses per-account TTL leases that are
# independent of the queue backend, so it carries no backend requirement.
_IMPLEMENTED_ROLES = ("all", "api", "background", "coordinator", "worker", "connector")
_RESERVED_ROLES: tuple[str, ...] = ()
_ALL_ROLES = _IMPLEMENTED_ROLES + _RESERVED_ROLES

# Roles whose whole reason to exist is the durable queue: the coordinator
# enqueues due work into it, the worker claims + executes from it. Both are a
# no-op (or worse, a silent degrade) under the embedded in-process scheduler, so
# they demand ``YURALUME_BACKGROUND_BACKEND=postgres`` at boot. ``connector`` is
# deliberately absent — messaging connectors lease per-account TTL rows, not
# queue jobs, so a connector process runs fine on any backend.
_DISTRIBUTED_ONLY_ROLES = ("coordinator", "worker")

# ``embedded`` = self-host default (in-process scheduler). ``postgres`` = P3-C
# hosted opt-in: the coordinator/worker execute real background work against the
# durable queue. It requires a database (fail-fast in ``settings.py``, same style
# as shadow). The distributed *roles* (coordinator/worker/connector) stay reserved
# — execution runs inside the existing ``background`` role's process for now.
_IMPLEMENTED_BACKENDS = ("embedded", "postgres")


@dataclass(frozen=True, slots=True)
class ProcessSettings:
    """Which process role this Core instance runs as, and the Phase 1
    realtime-relay wiring the ``background`` role needs to reach the api
    process (§7.0).

    Defaults are the self-host red line: ``all`` + ``embedded`` with no
    relay, i.e. the historical single-container behaviour.
    """

    role: str = "all"
    background_backend: str = "embedded"
    # P2-B shadow runtime (HOSTED_CORE_SCALING §13 Phase 2). ``""`` = off (the
    # self-host default: no coordinator/worker tasks, no tick journal, no queue
    # metrics series). ``"postgres"`` shadows the embedded scheduler against the
    # durable queue for the §14 comparison. Requires a database when enabled.
    background_shadow: str = ""
    # Phase 4 realtime backend (§7.1). ``memory`` = self-host default (in-process
    # EventBus, no outbox). ``postgres`` = distributed opt-in: the transactional
    # outbox + NOTIFY relay. Requires a database when enabled (validated in
    # ``settings.py``). Kept as a plain string so tests construct
    # ``ProcessSettings`` directly without env validation.
    realtime_backend: str = "memory"
    # Fallback poll cadence (seconds) for the api-side outbox dispatcher — the
    # safety net behind LISTEN/NOTIFY. Only consulted under the ``postgres``
    # realtime backend; the self-host default never builds a dispatcher.
    realtime_poll_interval: float = _DEFAULT_REALTIME_POLL_INTERVAL
    # Fallback poll cadence (seconds) for the runtime-config refresher, the
    # safety net behind the config-change LISTEN/NOTIFY. Consulted in every role
    # on a PostgreSQL deployment; the SQLite/self-host shape builds no refresher
    # at all, so the value is inert there.
    runtime_config_poll_interval: float = _DEFAULT_RUNTIME_CONFIG_POLL_INTERVAL
    # Per-channel bearer protecting GET /api/internal/v1/metrics (§12 / Phase
    # 0). Unset → the metrics route fail-closes with 503, so a scrape can never
    # reach an unprotected endpoint. Empty by default (self-host does not
    # expose the internal surface publicly).
    metrics_internal_token: str = ""


def load_process_settings() -> ProcessSettings:
    """Read + validate the process group from env (called by ``from_env``).

    Fail-fast with an actionable message naming the env var and the allowed
    values, mirroring the storage-validation pattern in ``settings.py``.
    Reserved roles and the not-yet-implemented ``postgres`` backend are
    rejected explicitly so a Hosted config typo surfaces at boot rather than
    silently degrading.
    """

    role = (os.getenv(_ROLE_ENV, "all").strip().lower() or "all")
    backend = (
        os.getenv(_BACKEND_ENV, "embedded").strip().lower() or "embedded"
    )
    metrics_token = os.getenv(_METRICS_TOKEN_ENV, "").strip()
    shadow = os.getenv(_SHADOW_ENV, "").strip().lower()
    realtime_backend = (
        os.getenv(_REALTIME_BACKEND_ENV, "memory").strip().lower() or "memory"
    )

    # Phase 4 (§7.1) removed the §7.0 HTTP relay: a config that still sets its
    # env is stale and would silently do nothing, so fail-fast pointing at the
    # replacement rather than boot a deployment whose SSE would go quietly dark.
    for retired_env in (_RELAY_URL_ENV, _RELAY_TOKEN_ENV):
        if os.getenv(retired_env, "").strip():
            raise ValueError(
                f"{retired_env} was removed in Phase 4 (the §7.0 realtime relay "
                "is gone); set YURALUME_REALTIME_BACKEND=postgres to use the "
                "durable realtime outbox instead",
            )

    if role not in _IMPLEMENTED_ROLES:
        raise ValueError(
            f"{_ROLE_ENV}={role} is not a valid process role; "
            f"allowed values: {', '.join(_ALL_ROLES)}",
        )

    if backend not in _IMPLEMENTED_BACKENDS:
        raise ValueError(
            f"{_BACKEND_ENV}={backend} is not a valid background backend; "
            "allowed values: embedded, postgres",
        )

    # The coordinator/worker roles only make sense against the durable queue —
    # under the embedded in-process scheduler they would either do nothing or
    # silently degrade. Fail-fast at boot, naming the backend override the
    # operator must set (the DB itself is then required by the postgres-backend
    # check in ``settings.py``, so distributed roles transitively require a DB).
    if role in _DISTRIBUTED_ONLY_ROLES and backend != "postgres":
        raise ValueError(
            f"{_ROLE_ENV}={role} only makes sense on the distributed queue "
            f"backend; set {_BACKEND_ENV}=postgres (these roles enqueue into / "
            "claim from the durable PostgreSQL queue and are a no-op under the "
            "embedded in-process scheduler)",
        )

    if shadow not in _IMPLEMENTED_SHADOWS:
        raise ValueError(
            f"{_SHADOW_ENV}={shadow} is not a valid shadow mode; "
            "allowed values: (empty), postgres",
        )

    if realtime_backend not in _IMPLEMENTED_REALTIME_BACKENDS:
        raise ValueError(
            f"{_REALTIME_BACKEND_ENV}={realtime_backend} is not a valid "
            "realtime backend; allowed values: memory, postgres",
        )

    poll_interval = _load_poll_interval()
    runtime_config_poll_interval = _load_positive_float(
        _RUNTIME_CONFIG_POLL_INTERVAL_ENV,
        _DEFAULT_RUNTIME_CONFIG_POLL_INTERVAL,
    )

    return ProcessSettings(
        role=role,
        background_backend=backend,
        metrics_internal_token=metrics_token,
        background_shadow=shadow,
        realtime_backend=realtime_backend,
        realtime_poll_interval=poll_interval,
        runtime_config_poll_interval=runtime_config_poll_interval,
    )


def _load_poll_interval() -> float:
    """Read the dispatcher fallback-poll cadence, fail-fast on garbage."""
    return _load_positive_float(
        _REALTIME_POLL_INTERVAL_ENV, _DEFAULT_REALTIME_POLL_INTERVAL,
    )


def _load_positive_float(env_key: str, default: float) -> float:
    """Read a positive float knob from env, fail-fast on garbage.

    Absent / blank → ``default``; a non-numeric, non-finite or non-positive
    value is a config mistake that would either crash the consuming loop or
    hot-loop it, so it stops the process at boot with a message naming the env
    var (same fail-fast discipline as the DB pool knobs in ``settings.py``).

    ``NaN`` and ``Infinity`` need their own check: ``float()`` parses both, and
    ``NaN <= 0`` is False, so the positivity test alone waved them through. A
    NaN cadence makes every ``asyncio.wait_for`` raise and an infinite one parks
    the fallback poll forever — a knob that silently disables the safety net it
    is supposed to tune.
    """
    raw = os.getenv(env_key)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw.strip())
    except ValueError:
        raise ValueError(
            f"{env_key} must be a positive number of seconds (got {raw!r})",
        )
    if not math.isfinite(value):
        raise ValueError(
            f"{env_key} must be a finite number of seconds (got {raw!r})",
        )
    if value <= 0:
        raise ValueError(f"{env_key} must be > 0 (got {value})")
    return value
