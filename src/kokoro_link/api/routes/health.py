"""Liveness probe (HOSTED_CORE_SCALING §2.1 — role-aware health).

``GET /health`` is what the compose healthchecks and ``deploy.sh``'s
health-wait poll. A flat 200 is a lie for the ``background`` role: that
process serves no API, so a 200 there only means "uvicorn is up" — it says
nothing about whether the singleton scheduler task is still alive. If the
scheduler crashes, a static 200 lets every downstream gate (healthcheck,
deploy health-wait) treat a process doing zero background work as healthy.

So the probe is loop-liveness-aware: a role that STARTS a core loop it owns
(the embedded schedulers under ``start_schedulers`` — ``all`` / ``background``;
the durable coordinator/worker under ``run_background_coordinator`` /
``run_background_worker`` — ``all`` / ``background`` / the dedicated
``coordinator`` / ``worker`` roles) 503s when that loop was started and its task
has since exited/crashed. Every non-started shape stays 200 to avoid false
negatives before boot: the ``api`` / ``connector`` roles (own no such loop),
bare containers (loops ``None``), and lifespan-not-run TestClients (the task was
never created).
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()


@router.get("/health")
def health(request: Request) -> JSONResponse:
    state = request.app.state
    matrix = getattr(state, "matrix", None)
    container = getattr(state, "container", None)
    # A role is "unhealthy" when a core loop it OWNS was started and has since
    # died. Each role only owns the loops its matrix flags turn on (§2.1): the
    # embedded schedulers ride with ``start_schedulers`` (all / background); the
    # durable coordinator/worker ride with ``run_background_coordinator`` /
    # ``run_background_worker`` — so a dedicated coordinator or worker process
    # 503s on ITS loop's death without falsely depending on an embedded scheduler
    # it never runs.
    if matrix is not None and container is not None:
        owned: list[object | None] = []
        if matrix.start_schedulers:
            owned.append(getattr(container, "proactive_scheduler", None))
            owned.append(getattr(container, "world_event_scheduler", None))
        if matrix.run_background_coordinator:
            owned.append(getattr(container, "background_shadow_coordinator", None))
        if matrix.run_background_worker:
            owned.append(getattr(container, "background_shadow_worker", None))
        for scheduler in owned:
            if _scheduler_exited(scheduler):
                return JSONResponse(
                    status_code=503,
                    content={
                        "status": "unhealthy",
                        "reason": "scheduler_exited",
                        "site_settings_overlay": _site_settings_overlay(container),
                    },
                )
    return JSONResponse(
        content={
            "status": "ok",
            "site_settings_overlay": _site_settings_overlay(container),
        },
    )


def _site_settings_overlay(container: object | None) -> str:
    """Where this process's site settings came from: ``db`` or ``env_fallback``.

    Diagnostic only — deliberately NOT part of the healthy/unhealthy verdict.
    The DB overlay is fail-soft by design (a fresh database has no table yet,
    and refusing to boot over it would be worse), but until now the degraded
    outcome was invisible: an operator whose Admin change "did nothing" had no
    way to tell a process that read the table and disagreed apart from one that
    never managed to read it at all. Failing the probe on it would turn a
    cosmetic staleness into a deploy-blocking outage, so it is reported, not
    enforced.

    ``unknown`` covers hand-built containers with no holder wired.
    """
    holder = getattr(container, "site_settings_holder", None)
    if holder is None:
        return "unknown"
    return str(getattr(holder, "overlay_source", "unknown"))


def _scheduler_exited(scheduler: object | None) -> bool:
    """A wired scheduler that was ``started`` but is no longer ``is_running``
    has exited/crashed. ``stop()`` nulls the task ref, so a clean shutdown reads
    as not-started (never as exited). ``None`` (unwired) and fakes/stubs without
    the liveness surface are treated as healthy — the probe must never crash on
    a partial container or a never-started task."""
    if scheduler is None:
        return False
    started = getattr(scheduler, "started", None)
    is_running = getattr(scheduler, "is_running", None)
    if started is None or is_running is None:
        return False
    return bool(started) and not bool(is_running)
