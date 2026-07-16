"""SQLAlchemy repository for Creator Studio generation jobs."""

from __future__ import annotations

import json
import logging
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker

from kokoro_link.contracts.clock import ensure_utc
from kokoro_link.contracts.studio_jobs import (
    JOB_STATUS_RUNNING,
    StudioGenerationJob,
)
from kokoro_link.infrastructure.persistence.models import (
    StudioGenerationJobRow,
)


_LOGGER = logging.getLogger(__name__)


class SAStudioJobRepository:
    def __init__(self, session_factory: sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def add(self, job: StudioGenerationJob) -> None:
        async with self._session_factory() as session:
            session.add(_to_row(job))
            await session.commit()

    async def save(self, job: StudioGenerationJob) -> None:
        async with self._session_factory() as session:
            row = await session.get(StudioGenerationJobRow, job.id)
            if row is None:
                session.add(_to_row(job))
            else:
                row.kind = job.kind
                row.target_id = job.target_id
                row.status = job.status
                row.attempts = job.attempts
                row.params_json = _serialize_params(job)
                row.error_message = job.error_message
                row.updated_at = ensure_utc(job.updated_at)
            await session.commit()

    async def get(self, job_id: str) -> StudioGenerationJob | None:
        async with self._session_factory() as session:
            row = await session.get(StudioGenerationJobRow, job_id)
            return _to_entity(row) if row is not None else None

    async def list_running(self) -> list[StudioGenerationJob]:
        stmt = (
            select(StudioGenerationJobRow)
            .where(StudioGenerationJobRow.status == JOB_STATUS_RUNNING)
            .order_by(StudioGenerationJobRow.created_at.asc())
        )
        async with self._session_factory() as session:
            result = await session.execute(stmt)
            return [_to_entity(row) for row in result.scalars()]

    async def delete_finished_before(self, cutoff: datetime) -> int:
        stmt = delete(StudioGenerationJobRow).where(
            StudioGenerationJobRow.status != JOB_STATUS_RUNNING,
            StudioGenerationJobRow.updated_at < ensure_utc(cutoff),
        )
        async with self._session_factory() as session:
            result = await session.execute(stmt)
            await session.commit()
            return int(result.rowcount or 0)


def _to_row(job: StudioGenerationJob) -> StudioGenerationJobRow:
    return StudioGenerationJobRow(
        id=job.id,
        kind=job.kind,
        target_id=job.target_id,
        status=job.status,
        attempts=job.attempts,
        params_json=_serialize_params(job),
        error_message=job.error_message,
        created_at=ensure_utc(job.created_at),
        updated_at=ensure_utc(job.updated_at),
    )


def _to_entity(row: StudioGenerationJobRow) -> StudioGenerationJob:
    return StudioGenerationJob(
        id=row.id,
        kind=row.kind,
        target_id=row.target_id,
        status=row.status,
        attempts=row.attempts,
        params=_parse_params(row),
        error_message=row.error_message,
        created_at=ensure_utc(row.created_at),
        updated_at=ensure_utc(row.updated_at),
    )


def _serialize_params(job: StudioGenerationJob) -> str:
    try:
        return json.dumps(dict(job.params), ensure_ascii=False)
    except (TypeError, ValueError):
        _LOGGER.exception(
            "studio job params not serializable job=%s", job.id,
        )
        return "{}"


def _parse_params(row: StudioGenerationJobRow) -> dict:
    raw = row.params_json or "{}"
    try:
        parsed = json.loads(raw)
    except ValueError:
        _LOGGER.warning(
            "studio job params_json unreadable job=%s", row.id,
        )
        return {}
    return parsed if isinstance(parsed, dict) else {}
