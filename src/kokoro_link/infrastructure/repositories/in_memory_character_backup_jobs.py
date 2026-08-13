"""In-process ``CharacterBackupJobRepositoryPort`` for tests / dev mode.

The lock stands in for the database's row-level atomicity so the SQL
twin's guarantees hold here too: per-character export dedup inside
``add``, ``running``-guarded checkpoints, and CAS finalization that
erases the payload as part of the same step.
"""

from __future__ import annotations

import threading
from dataclasses import replace
from datetime import datetime

from kokoro_link.contracts.character_backup_jobs import (
    BACKUP_JOB_KIND_EXPORT,
    BACKUP_JOB_KIND_RESTORE,
    BACKUP_JOB_STATUS_RUNNING,
    BackupJobConflictError,
    CharacterBackupJob,
)
from kokoro_link.contracts.clock import ensure_utc


class InMemoryCharacterBackupJobRepository:
    def __init__(self) -> None:
        self._jobs: dict[str, CharacterBackupJob] = {}
        self._lock = threading.RLock()

    async def add(self, job: CharacterBackupJob) -> None:
        with self._lock:
            if (
                job.kind == BACKUP_JOB_KIND_EXPORT
                and job.character_id is not None
                and self._active_export(job.character_id) is not None
            ):
                raise BackupJobConflictError(job.character_id)
            if (
                job.kind == BACKUP_JOB_KIND_RESTORE
                and self._active_restore(job.operator_id) is not None
            ):
                raise BackupJobConflictError(operator_id=job.operator_id)
            self._jobs[job.id] = job

    async def save_progress_if_running(
        self, job: CharacterBackupJob,
    ) -> bool:
        with self._lock:
            current = self._jobs.get(job.id)
            if (
                current is None
                or current.status != BACKUP_JOB_STATUS_RUNNING
            ):
                return False
            self._jobs[job.id] = replace(
                current,
                character_id=job.character_id,
                attempts=job.attempts,
                progress=dict(job.progress),
                payload=dict(job.payload),
                error=job.error,
                artifact_object_key=job.artifact_object_key,
                updated_at=job.updated_at,
            )
            return True

    async def finalize_scrubbed(self, job: CharacterBackupJob) -> bool:
        if job.status == BACKUP_JOB_STATUS_RUNNING:
            raise ValueError(
                "finalize_scrubbed needs a terminal job status, "
                f"got {job.status!r}",
            )
        with self._lock:
            current = self._jobs.get(job.id)
            if (
                current is None
                or current.status != BACKUP_JOB_STATUS_RUNNING
            ):
                return False
            self._jobs[job.id] = replace(
                job,
                # Key erasure mirrors the SQL twin: the stored payload is
                # dropped no matter what the entity carries.
                payload={},
                finished_at=job.finished_at or job.updated_at,
            )
            return True

    async def get(self, job_id: str) -> CharacterBackupJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    async def get_active_export_for_character(
        self, character_id: str,
    ) -> CharacterBackupJob | None:
        with self._lock:
            return self._active_export(character_id)

    async def list_running(self) -> list[CharacterBackupJob]:
        with self._lock:
            running = [
                job for job in self._jobs.values()
                if job.status == BACKUP_JOB_STATUS_RUNNING
            ]
        return sorted(running, key=lambda job: job.created_at)

    async def scrub_payloads_older_than(self, cutoff: datetime) -> int:
        cutoff_utc = ensure_utc(cutoff)
        scrubbed = 0
        with self._lock:
            for job_id, job in self._jobs.items():
                if (
                    ensure_utc(job.created_at) < cutoff_utc
                    and dict(job.payload)
                ):
                    self._jobs[job_id] = replace(job, payload={})
                    scrubbed += 1
        return scrubbed

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

    def _active_export(
        self, character_id: str,
    ) -> CharacterBackupJob | None:
        for job in self._jobs.values():
            if (
                job.kind == BACKUP_JOB_KIND_EXPORT
                and job.character_id == character_id
                and job.status == BACKUP_JOB_STATUS_RUNNING
            ):
                return job
        return None

    def _active_restore(
        self, operator_id: str,
    ) -> CharacterBackupJob | None:
        for job in self._jobs.values():
            if (
                job.kind == BACKUP_JOB_KIND_RESTORE
                and job.operator_id == operator_id
                and job.status == BACKUP_JOB_STATUS_RUNNING
            ):
                return job
        return None
