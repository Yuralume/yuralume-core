"""SQLAlchemy repository for character backup / restore jobs (CB0).

Mirrors ``sa_studio_job_repository`` for the CAS shape, with the two
backup-specific semantics living here rather than in callers:

- per-character in-flight-export dedup (``add`` raises the typed
  conflict; the partial unique index is the cross-instance backstop),
- key erasure on every terminal transition (``finalize_scrubbed`` sets
  ``payload_json = '{}'`` in the same UPDATE) plus the TTL backstop.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Mapping

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker

from kokoro_link.contracts.character_backup_jobs import (
    BACKUP_JOB_KIND_EXPORT,
    BACKUP_JOB_KIND_RESTORE,
    BACKUP_JOB_STATUS_RUNNING,
    BackupJobConflictError,
    CharacterBackupJob,
)
from kokoro_link.contracts.clock import ensure_utc
from kokoro_link.infrastructure.persistence.character_backup_models import (
    CharacterBackupJobRow,
)


_LOGGER = logging.getLogger(__name__)

_SCRUBBED_PAYLOAD_JSON = "{}"


class SACharacterBackupJobRepository:
    def __init__(
        self, session_factory: sessionmaker[AsyncSession],
    ) -> None:
        self._session_factory = session_factory

    async def add(self, job: CharacterBackupJob) -> None:
        """Insert; raises :class:`BackupJobConflictError` on a duplicate
        in-flight export for the same character, or a duplicate in-flight
        restore for the same operator (A2).

        The pre-insert SELECT makes the dedup a repository guarantee on
        any dialect; the partial unique indexes (PG + SQLite) additionally
        close the two-writers race, whose ``IntegrityError`` is
        translated to the same typed conflict.
        """
        async with self._session_factory() as session:
            if (
                job.kind == BACKUP_JOB_KIND_EXPORT
                and job.character_id is not None
            ):
                existing = await session.execute(
                    _active_export_stmt(job.character_id),
                )
                if existing.scalars().first() is not None:
                    raise BackupJobConflictError(job.character_id)
            if job.kind == BACKUP_JOB_KIND_RESTORE:
                existing = await session.execute(
                    _active_restore_stmt(job.operator_id),
                )
                if existing.scalars().first() is not None:
                    raise BackupJobConflictError(
                        operator_id=job.operator_id,
                    )
            session.add(_to_row(job))
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                if (
                    job.kind == BACKUP_JOB_KIND_EXPORT
                    and job.character_id is not None
                ):
                    raise BackupJobConflictError(
                        job.character_id,
                    ) from exc
                if job.kind == BACKUP_JOB_KIND_RESTORE:
                    raise BackupJobConflictError(
                        operator_id=job.operator_id,
                    ) from exc
                raise

    async def save_progress_if_running(
        self, job: CharacterBackupJob,
    ) -> bool:
        stmt = (
            update(CharacterBackupJobRow)
            .where(
                CharacterBackupJobRow.id == job.id,
                CharacterBackupJobRow.status == BACKUP_JOB_STATUS_RUNNING,
            )
            .values(
                character_id=job.character_id,
                attempts=job.attempts,
                progress_json=_dump_json(job.progress, job.id, "progress"),
                payload_json=_dump_json(job.payload, job.id, "payload"),
                error=job.error,
                artifact_object_key=job.artifact_object_key,
                updated_at=ensure_utc(job.updated_at),
            )
        )
        async with self._session_factory() as session:
            result = await session.execute(stmt)
            await session.commit()
            return bool(result.rowcount)

    async def finalize_scrubbed(self, job: CharacterBackupJob) -> bool:
        if job.status == BACKUP_JOB_STATUS_RUNNING:
            raise ValueError(
                "finalize_scrubbed needs a terminal job status, "
                f"got {job.status!r}",
            )
        finished_at = ensure_utc(job.finished_at or job.updated_at)
        stmt = (
            update(CharacterBackupJobRow)
            .where(
                CharacterBackupJobRow.id == job.id,
                CharacterBackupJobRow.status == BACKUP_JOB_STATUS_RUNNING,
            )
            .values(
                character_id=job.character_id,
                status=job.status,
                attempts=job.attempts,
                progress_json=_dump_json(job.progress, job.id, "progress"),
                # Key erasure is THIS statement's semantic — the stored
                # payload is dropped no matter what the entity carries.
                payload_json=_SCRUBBED_PAYLOAD_JSON,
                error=job.error,
                artifact_object_key=job.artifact_object_key,
                updated_at=ensure_utc(job.updated_at),
                finished_at=finished_at,
            )
        )
        async with self._session_factory() as session:
            result = await session.execute(stmt)
            await session.commit()
            return bool(result.rowcount)

    async def get(self, job_id: str) -> CharacterBackupJob | None:
        async with self._session_factory() as session:
            row = await session.get(CharacterBackupJobRow, job_id)
            return _to_entity(row) if row is not None else None

    async def get_active_export_for_character(
        self, character_id: str,
    ) -> CharacterBackupJob | None:
        async with self._session_factory() as session:
            result = await session.execute(
                _active_export_stmt(character_id),
            )
            row = result.scalars().first()
            return _to_entity(row) if row is not None else None

    async def list_running(self) -> list[CharacterBackupJob]:
        stmt = (
            select(CharacterBackupJobRow)
            .where(
                CharacterBackupJobRow.status == BACKUP_JOB_STATUS_RUNNING,
            )
            .order_by(CharacterBackupJobRow.created_at.asc())
        )
        async with self._session_factory() as session:
            result = await session.execute(stmt)
            return [_to_entity(row) for row in result.scalars()]

    async def scrub_payloads_older_than(self, cutoff: datetime) -> int:
        stmt = (
            update(CharacterBackupJobRow)
            .where(
                CharacterBackupJobRow.created_at < ensure_utc(cutoff),
                CharacterBackupJobRow.payload_json
                != _SCRUBBED_PAYLOAD_JSON,
            )
            .values(payload_json=_SCRUBBED_PAYLOAD_JSON)
        )
        async with self._session_factory() as session:
            result = await session.execute(stmt)
            await session.commit()
            return int(result.rowcount or 0)

    async def delete_finished_before(self, cutoff: datetime) -> int:
        stmt = delete(CharacterBackupJobRow).where(
            CharacterBackupJobRow.status != BACKUP_JOB_STATUS_RUNNING,
            CharacterBackupJobRow.updated_at < ensure_utc(cutoff),
        )
        async with self._session_factory() as session:
            result = await session.execute(stmt)
            await session.commit()
            return int(result.rowcount or 0)


def _active_export_stmt(character_id: str):  # noqa: ANN202
    return (
        select(CharacterBackupJobRow)
        .where(
            CharacterBackupJobRow.character_id == character_id,
            CharacterBackupJobRow.kind == BACKUP_JOB_KIND_EXPORT,
            CharacterBackupJobRow.status == BACKUP_JOB_STATUS_RUNNING,
        )
        .limit(1)
    )


def _active_restore_stmt(operator_id: str):  # noqa: ANN202
    return (
        select(CharacterBackupJobRow)
        .where(
            CharacterBackupJobRow.operator_id == operator_id,
            CharacterBackupJobRow.kind == BACKUP_JOB_KIND_RESTORE,
            CharacterBackupJobRow.status == BACKUP_JOB_STATUS_RUNNING,
        )
        .limit(1)
    )


def _to_row(job: CharacterBackupJob) -> CharacterBackupJobRow:
    return CharacterBackupJobRow(
        id=job.id,
        kind=job.kind,
        character_id=job.character_id,
        operator_id=job.operator_id,
        status=job.status,
        attempts=job.attempts,
        progress_json=_dump_json(job.progress, job.id, "progress"),
        payload_json=_dump_json(job.payload, job.id, "payload"),
        error=job.error,
        artifact_object_key=job.artifact_object_key,
        created_at=ensure_utc(job.created_at),
        updated_at=ensure_utc(job.updated_at),
        finished_at=(
            ensure_utc(job.finished_at)
            if job.finished_at is not None
            else None
        ),
    )


def _to_entity(row: CharacterBackupJobRow) -> CharacterBackupJob:
    return CharacterBackupJob(
        id=row.id,
        kind=row.kind,
        character_id=row.character_id,
        operator_id=row.operator_id,
        status=row.status,
        attempts=row.attempts,
        progress=_parse_json(row.progress_json, row.id, "progress"),
        payload=_parse_json(row.payload_json, row.id, "payload"),
        error=row.error,
        artifact_object_key=row.artifact_object_key,
        created_at=ensure_utc(row.created_at),
        updated_at=ensure_utc(row.updated_at),
        finished_at=(
            ensure_utc(row.finished_at)
            if row.finished_at is not None
            else None
        ),
    )


def _dump_json(
    payload: Mapping[str, Any], job_id: str, label: str,
) -> str:
    try:
        return json.dumps(dict(payload), ensure_ascii=False)
    except (TypeError, ValueError):
        _LOGGER.exception(
            "backup job %s not serializable job=%s", label, job_id,
        )
        return "{}"


def _parse_json(raw: str, job_id: str, label: str) -> dict:
    try:
        parsed = json.loads(raw or "{}")
    except ValueError:
        _LOGGER.warning(
            "backup job %s unreadable job=%s", label, job_id,
        )
        return {}
    return parsed if isinstance(parsed, dict) else {}
