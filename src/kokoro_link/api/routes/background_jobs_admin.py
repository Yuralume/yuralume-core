"""Admin diagnostics for the P2-B shadow queue (HOSTED_CORE_SCALING §11 / §12).

Read-only queue stats + dead-letter listing + manual redrive, all admin-gated
(mirrors ``observability.py``: ``require_admin`` at the router level so a new
route can't skip the guard). Job operations require admin auth and are audited
(§11) — ``redrive`` logs an INFO line with the admin identity and job id.

Mounted under ``/api/v1`` in api-serving roles. When shadow is off the queue
port is unwired: stats/dead degrade to an explicit empty shape (``shadow_enabled
=false``) so the admin panel renders cleanly, while redrive — a mutation with
nothing to act on — returns 503.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from kokoro_link.api.dependencies import get_container, require_admin
from kokoro_link.bootstrap.container import ServiceContainer
from kokoro_link.contracts.background_jobs import BackgroundJob
from kokoro_link.domain.entities.operator_profile import OperatorProfile

_LOGGER = logging.getLogger(__name__)

# Audit channel for job operations (§11). A dedicated logger name so an operator
# can grep / route the queue-mutation audit trail independently.
_AUDIT_LOGGER = logging.getLogger("kokoro_link.audit.background_jobs")

router = APIRouter(
    tags=["background-jobs-admin"], dependencies=[Depends(require_admin)],
)


class ShadowCountersResponse(BaseModel):
    enqueued: int = 0
    deduped: int = 0
    claimed: int = 0
    completed: int = 0
    skipped: int = 0
    failed: int = 0
    lease_lost: int = 0


class LeaseInfoResponse(BaseModel):
    owner_id: str
    epoch: int
    lease_until: str
    held: bool


class QueueStatsResponse(BaseModel):
    shadow_enabled: bool
    status_counts: dict[str, int] = Field(default_factory=dict)
    kind_counts: dict[str, int] = Field(default_factory=dict)
    oldest_queued_age_seconds: float = 0.0
    total: int = 0
    coordinator: ShadowCountersResponse | None = None
    worker: ShadowCountersResponse | None = None
    lease: LeaseInfoResponse | None = None


class BackgroundJobResponse(BaseModel):
    id: str
    kind: str
    status: str
    idempotency_key: str
    priority: int
    fencing_epoch: int
    attempt_count: int
    max_attempts: int
    character_id: str | None = None
    tenant_id: str | None = None
    operator_id: str | None = None
    last_error: str | None = None
    created_at: str
    updated_at: str
    finished_at: str | None = None

    @classmethod
    def from_domain(cls, job: BackgroundJob) -> "BackgroundJobResponse":
        return cls(
            id=job.id,
            kind=job.kind,
            status=job.status.value,
            idempotency_key=job.idempotency_key,
            priority=job.priority,
            fencing_epoch=job.fencing_epoch,
            attempt_count=job.attempt_count,
            max_attempts=job.max_attempts,
            character_id=job.character_id,
            tenant_id=job.tenant_id,
            operator_id=job.operator_id,
            last_error=job.last_error,
            created_at=job.created_at.isoformat(),
            updated_at=job.updated_at.isoformat(),
            finished_at=job.finished_at.isoformat() if job.finished_at else None,
        )


class RedriveResponse(BaseModel):
    job_id: str
    redriven: bool


def _counters_response(service) -> ShadowCountersResponse | None:
    if service is None:
        return None
    snap = service.snapshot()
    return ShadowCountersResponse(**snap.as_dict())


@router.get(
    "/admin/background-jobs/stats",
    response_model=QueueStatsResponse,
)
async def background_jobs_stats(
    container: ServiceContainer = Depends(get_container),
) -> QueueStatsResponse:
    """Live queue stats + shadow service counters + coordinator lease.

    Shadow off (queue unwired) → ``shadow_enabled=false`` with an otherwise
    empty shape rather than a 404, so the admin panel can render a stable
    "shadow disabled" state without special-casing the HTTP status."""
    from datetime import datetime, timezone

    from kokoro_link.contracts.background_jobs import COORDINATOR_LEASE_NAME
    from kokoro_link.contracts.clock import ensure_utc

    queue = container.background_job_queue
    if queue is None:
        return QueueStatsResponse(shadow_enabled=False)
    now = ensure_utc(datetime.now(timezone.utc))
    stats = await queue.stats(now=now)
    lease_port = container.background_coordinator_lease
    lease_response: LeaseInfoResponse | None = None
    if lease_port is not None:
        current = await lease_port.current(COORDINATOR_LEASE_NAME)
        if current is not None:
            owner_id, epoch, lease_until = current
            lease_response = LeaseInfoResponse(
                owner_id=owner_id,
                epoch=epoch,
                lease_until=ensure_utc(lease_until).isoformat(),
                held=ensure_utc(lease_until) > now,
            )
    return QueueStatsResponse(
        shadow_enabled=True,
        status_counts=dict(stats.status_counts),
        kind_counts=dict(stats.kind_counts),
        oldest_queued_age_seconds=stats.oldest_queued_age_seconds,
        total=stats.total,
        coordinator=_counters_response(container.background_shadow_coordinator),
        worker=_counters_response(container.background_shadow_worker),
        lease=lease_response,
    )


@router.get(
    "/admin/background-jobs/dead",
    response_model=list[BackgroundJobResponse],
)
async def background_jobs_dead(
    limit: int = Query(default=50, ge=1, le=500),
    container: ServiceContainer = Depends(get_container),
) -> list[BackgroundJobResponse]:
    """Dead-letter listing. Shadow off → empty list (nothing to inspect)."""
    queue = container.background_job_queue
    if queue is None:
        return []
    dead = await queue.list_dead(limit=limit)
    return [BackgroundJobResponse.from_domain(job) for job in dead]


@router.post(
    "/admin/background-jobs/{job_id}/redrive",
    response_model=RedriveResponse,
)
async def background_jobs_redrive(
    job_id: str,
    admin: OperatorProfile = Depends(require_admin),
    container: ServiceContainer = Depends(get_container),
) -> RedriveResponse:
    """Admin manual redrive of a dead job (§11 — auth + audit).

    A mutation, so shadow-off (no queue) is a 503 rather than a silent no-op.
    Every call is audited with the admin identity + job id regardless of
    outcome so the trail records the attempt, not just the successes."""
    queue = container.background_job_queue
    if queue is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="background shadow queue is not wired",
        )
    redriven = await queue.redrive(job_id)
    _AUDIT_LOGGER.info(
        "background job redrive admin=%s job_id=%s result=%s",
        admin.id, job_id, "redriven" if redriven else "noop",
    )
    return RedriveResponse(job_id=job_id, redriven=redriven)
