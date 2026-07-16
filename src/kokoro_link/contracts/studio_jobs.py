"""Ports for durable Creator Studio generation jobs.

Fusion-story and branching-drama pipelines run as in-process
``asyncio.Task``s; without a durable record a service restart silently
drops whatever was in flight and the story/drama row is stuck on a
non-terminal status forever. Each spawned pipeline therefore writes a
``StudioGenerationJob`` row first, finalizes it when the pipeline ends,
and startup recovery re-drives whatever is still ``running``.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Mapping, Protocol
from uuid import uuid4


JOB_KIND_FUSION_CREATE = "fusion_create"
JOB_KIND_FUSION_ITERATE_OUTLINE = "fusion_iterate_outline"
JOB_KIND_FUSION_ITERATE_BEAT = "fusion_iterate_beat"
JOB_KIND_FUSION_POLISH = "fusion_polish"
JOB_KIND_BRANCHING_CREATE = "branching_create"

FUSION_JOB_KINDS = frozenset({
    JOB_KIND_FUSION_CREATE,
    JOB_KIND_FUSION_ITERATE_OUTLINE,
    JOB_KIND_FUSION_ITERATE_BEAT,
    JOB_KIND_FUSION_POLISH,
})
BRANCHING_JOB_KINDS = frozenset({JOB_KIND_BRANCHING_CREATE})

JOB_STATUS_RUNNING = "running"
JOB_STATUS_SUCCEEDED = "succeeded"
JOB_STATUS_FAILED = "failed"

_VALID_JOB_STATUSES = frozenset({
    JOB_STATUS_RUNNING,
    JOB_STATUS_SUCCEEDED,
    JOB_STATUS_FAILED,
})

MAX_JOB_ATTEMPTS = 3
"""Total pipeline runs a job may consume (first run + restart resumes).
A job that keeps getting interrupted is most likely crashing the
process itself — after this many attempts recovery stops resuming and
fails the story/drama with a retry hint instead of crash-looping."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class StudioGenerationJob:
    """Durable record of one background generation pipeline run."""

    id: str
    kind: str
    target_id: str
    """Fusion story id or branching drama id — intentionally not a FK
    so a deleted target degrades to a failed job, never a broken row."""
    status: str
    attempts: int
    params: Mapping[str, Any] = field(default_factory=dict)
    """Operation inputs recovery needs to re-drive the pipeline
    (``beat_index`` / ``hint`` / ``operator_primary_language``)."""
    error_message: str | None = None
    created_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("StudioGenerationJob.id must be non-empty")
        if not self.kind:
            raise ValueError("StudioGenerationJob.kind must be non-empty")
        if not self.target_id:
            raise ValueError(
                "StudioGenerationJob.target_id must be non-empty",
            )
        if self.status not in _VALID_JOB_STATUSES:
            raise ValueError(
                f"StudioGenerationJob.status {self.status!r} must be one "
                f"of {sorted(_VALID_JOB_STATUSES)}",
            )
        if self.attempts < 1:
            raise ValueError("StudioGenerationJob.attempts must be >= 1")

    @classmethod
    def create(
        cls,
        *,
        kind: str,
        target_id: str,
        params: Mapping[str, Any] | None = None,
    ) -> "StudioGenerationJob":
        now = _utcnow()
        return cls(
            id=uuid4().hex,
            kind=kind,
            target_id=target_id,
            status=JOB_STATUS_RUNNING,
            attempts=1,
            params=dict(params or {}),
            error_message=None,
            created_at=now,
            updated_at=now,
        )

    def with_status(
        self,
        status: str,
        *,
        error_message: str | None = None,
    ) -> "StudioGenerationJob":
        return replace(
            self,
            status=status,
            error_message=error_message,
            updated_at=_utcnow(),
        )

    def with_attempts(self, attempts: int) -> "StudioGenerationJob":
        return replace(self, attempts=attempts, updated_at=_utcnow())

    def is_finished(self) -> bool:
        return self.status != JOB_STATUS_RUNNING


class StudioJobRepositoryPort(Protocol):
    """Persistence for :class:`StudioGenerationJob` rows."""

    async def add(self, job: StudioGenerationJob) -> None: ...

    async def save(self, job: StudioGenerationJob) -> None: ...

    async def get(self, job_id: str) -> StudioGenerationJob | None: ...

    async def list_running(self) -> list[StudioGenerationJob]: ...

    async def delete_finished_before(self, cutoff: datetime) -> int: ...
