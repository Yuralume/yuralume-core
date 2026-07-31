"""Internal Prometheus metrics endpoint (HOSTED_CORE_SCALING §12 / Phase 0).

``GET /api/internal/v1/metrics`` — the scrape target the hosted Cloud
Prometheus hits over host loopback to observe scheduler tick timing, character
gauges, and build info. Free of ``prometheus_client``: the registry renders the
text exposition format itself (see ``infrastructure/observability/scheduler_metrics``).

Auth follows the repo's per-channel internal-token convention (mirrors
``internal_cloud``): a dedicated bearer token parsed at
request time, fail-closed — 503 when the channel is unconfigured, 401 on a
bad/missing token, constant-time compare. Registered in every process role
whose component matrix sets ``serve_metrics_route`` (background included, so the
singleton scheduler's timings are scrapeable even though it serves no API).
"""

from __future__ import annotations

import logging
import os
import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.responses import PlainTextResponse

from kokoro_link.api.dependencies import get_container
from kokoro_link.bootstrap.container import ServiceContainer
from kokoro_link.contracts.background_jobs import COORDINATOR_LEASE_NAME
from kokoro_link.contracts.clock import ensure_utc
from kokoro_link.infrastructure.observability.action_billing_metrics import (
    render_action_billing_metrics,
)
from kokoro_link.infrastructure.observability.background_queue_metrics import (
    render_queue_metrics,
)
from kokoro_link.infrastructure.observability.drain_metrics import (
    render_drain_metrics,
)
from kokoro_link.infrastructure.observability.execution_mode_metrics import (
    render_execution_mode_metrics,
)
from kokoro_link.infrastructure.observability.prompt_pack_metrics import (
    render_prompt_pack_metrics,
)
from kokoro_link.infrastructure.observability.scheduler_metrics import (
    SchedulerMetrics,
)

_LOGGER = logging.getLogger(__name__)

router = APIRouter(tags=["internal-metrics"])

_INTERNAL_TOKEN_ENV = "YURALUME_METRICS_INTERNAL_TOKEN"
_PROMETHEUS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


def _configured_token() -> str:
    "Parse the metrics scrape bearer token at request time (mirrors internal_cloud)."
    return os.getenv(_INTERNAL_TOKEN_ENV, "").strip()


def _extract_bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    presented = parts[1].strip()
    return presented or None


async def require_metrics_token(
    authorization: str | None = Header(default=None),
) -> None:
    configured = _configured_token()
    if not configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="metrics channel not configured",
        )
    presented = _extract_bearer(authorization)
    # Compare UTF-8 bytes so a non-ASCII presented token can't raise a 500 out
    # of compare_digest (same guard as internal_cloud).
    if presented is not None and secrets.compare_digest(
        presented.encode("utf-8"), configured.encode("utf-8"),
    ):
        return
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="invalid metrics token",
    )


@router.get(
    "/metrics",
    dependencies=[Depends(require_metrics_token)],
    response_class=PlainTextResponse,
)
async def scrape_metrics(
    container: ServiceContainer = Depends(get_container),
) -> PlainTextResponse:
    """Render the scheduler metrics registry in Prometheus text format.

    A bare container without a wired registry (test harnesses) renders a fresh
    empty registry — ``ticks_total 0`` + build info — rather than 500'ing."""
    registry = container.scheduler_metrics or SchedulerMetrics()
    body = registry.render_prometheus()
    queue_body = await _render_queue_metrics(container)
    if queue_body:
        body = body + queue_body
    mode_body = await _render_execution_mode_metrics(container)
    if mode_body:
        body = body + mode_body
    billing_body = _render_action_billing_metrics(container)
    if billing_body:
        body = body + billing_body
    pack_body = _render_prompt_pack_metrics(container)
    if pack_body:
        body = body + pack_body
    drain_body = _render_drain_metrics(container)
    if drain_body:
        body = body + drain_body
    return PlainTextResponse(body, media_type=_PROMETHEUS_CONTENT_TYPE)


def _render_drain_metrics(container: ServiceContainer) -> str:
    """Append this replica's drain switch and in-flight turn count (GD1-A).

    Unconditional and synchronous — two in-process integers, no port to await.
    ``yuralume_core_active_turns`` is the gauge ``deploy.sh`` polls to zero
    before it recreates a drained replica, so it is the one series here whose
    absence would silently turn a graceful roll back into the SIGKILL roll it
    replaced. Absent only on a container built without the field, and a read
    failure is swallowed like every sibling: the scrape must not be the thing
    that breaks.
    """
    state = getattr(container, "drain_state", None)
    if state is None:
        return ""
    try:
        return render_drain_metrics(
            draining=state.draining, active_turns=state.active_turns,
        )
    except Exception:  # never let a drain read break the scrape
        _LOGGER.exception("internal metrics: drain render failed")
        return ""


def _render_prompt_pack_metrics(container: ServiceContainer) -> str:
    """Append this process's prompt pack identity (PD3).

    Unconditional — hosted and self-host alike. The scrape is token-gated and
    loopback-only, which is exactly why the hash lives here and **not** on the
    public ``/health`` payload: a public build fingerprint helps only someone
    probing the deployment.

    Rendered once per process and cached by the exporter (the loader owns the
    pack for the process lifetime; there is no hot reload). Any failure to read
    the pack is swallowed and logged, same as the sibling helpers — a metrics
    scrape must not be the thing that breaks.
    """
    settings = getattr(container, "app_settings", None)
    try:
        return render_prompt_pack_metrics(
            humanization=getattr(settings, "humanization", None),
            prompt_quality=getattr(settings, "prompt_quality", None),
        )
    except Exception:  # never let a pack read break the scrape
        _LOGGER.exception("internal metrics: prompt pack render failed")
        return ""


def _render_action_billing_metrics(container: ServiceContainer) -> str:
    """Append the AP2/AP4 action-billing counters (AP5 residual R-2).

    Synchronous — these are in-process integers, not a port read, so unlike the
    queue and ownership series there is nothing to await. Absent on self-host,
    where the null billing service owns no counters.

    Two of the emitted series are **alert lines, not statistics**:
    ``yuralume_action_billing_released_uncovered`` (the Gateway is not
    honouring Core's interaction header, so a fixed-price tier is silently
    running on per-call billing) and
    ``yuralume_action_billing_close_abandoned`` (money left reserved on a
    player's wallet for the upstream sweeper). Both should sit at zero; see
    ``infrastructure/observability/action_billing_metrics``.
    """
    try:
        return render_action_billing_metrics(
            billing=getattr(
                getattr(container, "action_billing_service", None),
                "counters", None,
            ),
            overage=getattr(
                getattr(container, "quota_overage_service", None),
                "counters", None,
            ),
        )
    except Exception:  # never let a counter read break the scrape
        _LOGGER.exception("internal metrics: action billing render failed")
        return ""


async def _render_queue_metrics(container: ServiceContainer) -> str:
    """Append the P2-B shadow-queue series when the queue port is wired.

    Awaits ``queue.stats`` / ``lease.current`` live at scrape time so the
    numbers are current. Returns ``""`` when shadow is off (no queue) or on any
    read error — the scheduler metrics must still render on their own."""
    queue = getattr(container, "background_job_queue", None)
    if queue is None:
        return ""
    try:
        now = ensure_utc(datetime.now(timezone.utc))
        stats = await queue.stats(now=now)
        lease_port = getattr(container, "background_coordinator_lease", None)
        lease = (
            await lease_port.current(COORDINATOR_LEASE_NAME)
            if lease_port is not None else None
        )
        lease_held = bool(
            lease is not None and ensure_utc(lease[2]) > now,
        )
        coordinator = getattr(container, "background_shadow_coordinator", None)
        worker = getattr(container, "background_shadow_worker", None)
        return render_queue_metrics(
            stats=stats,
            coordinator=coordinator.snapshot() if coordinator else None,
            worker=worker.snapshot() if worker else None,
            lease=lease,
            lease_held=lease_held,
        )
    except Exception:  # never let a queue read break the scheduler scrape
        _LOGGER.exception("internal metrics: shadow queue render failed")
        return ""


async def _render_execution_mode_metrics(container: ServiceContainer) -> str:
    """Append the execution-ownership series when the ownership port is wired.

    Only present on the hosted opt-in (the port is None on self-host), so the
    series never surface unless enforcement is active. Reads the live mode/epoch
    at scrape time; the skip counter comes from the scheduler. Returns ``""`` on
    an unwired port or any read error — never breaks the scheduler scrape."""
    port = getattr(container, "runtime_ownership", None)
    if port is None:
        return ""
    try:
        mode, epoch = await port.get()
        scheduler = getattr(container, "proactive_scheduler", None)
        skips = getattr(scheduler, "ownership_skips_total", 0)
        return render_execution_mode_metrics(
            mode=mode, epoch=epoch, ownership_skips_total=skips,
        )
    except Exception:
        _LOGGER.exception("internal metrics: execution mode render failed")
        return ""
