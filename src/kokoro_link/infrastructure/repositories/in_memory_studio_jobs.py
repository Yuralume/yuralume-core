"""In-process ``StudioJobRepositoryPort`` for tests and dev mode."""

from __future__ import annotations

import threading
from datetime import datetime

from kokoro_link.contracts.clock import ensure_utc
from kokoro_link.contracts.studio_jobs import (
    JOB_STATUS_RUNNING,
    StudioGenerationJob,
)


class InMemoryStudioJobRepository:
    def __init__(self) -> None:
        self._jobs: dict[str, StudioGenerationJob] = {}
        self._lock = threading.RLock()

    async def add(self, job: StudioGenerationJob) -> None:
        with self._lock:
            self._jobs[job.id] = job

    async def save(self, job: StudioGenerationJob) -> None:
        with self._lock:
            self._jobs[job.id] = job

    async def get(self, job_id: str) -> StudioGenerationJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    async def list_running(self) -> list[StudioGenerationJob]:
        with self._lock:
            running = [
                job for job in self._jobs.values()
                if job.status == JOB_STATUS_RUNNING
            ]
        return sorted(running, key=lambda job: job.created_at)

    async def delete_finished_before(self, cutoff: datetime) -> int:
        cutoff_utc = ensure_utc(cutoff)
        with self._lock:
            stale = [
                job_id
                for job_id, job in self._jobs.items()
                if job.is_finished()
                and ensure_utc(job.updated_at) < cutoff_utc
            ]
            for job_id in stale:
                del self._jobs[job_id]
        return len(stale)
