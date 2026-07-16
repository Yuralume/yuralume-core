"""SQLAlchemy repos for the A/B experiment framework (HUMANIZATION_ROADMAP §4.6)."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker

from kokoro_link.contracts.experiment import (
    ExperimentAssignmentRepositoryPort,
    ExperimentRepositoryPort,
)
from kokoro_link.domain.entities.experiment import (
    Experiment,
    ExperimentAssignment,
    ExperimentVariant,
)
from kokoro_link.infrastructure.persistence.models import (
    ExperimentAssignmentRow,
    ExperimentRow,
)


def _ensure_utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _variants_to_json(variants: tuple[ExperimentVariant, ...]) -> str:
    return json.dumps(
        [{"id": v.id, "label": v.label} for v in variants],
        ensure_ascii=False,
    )


def _variants_from_json(raw: str | None) -> tuple[ExperimentVariant, ...]:
    if not raw:
        return ()
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return ()
    if not isinstance(data, list):
        return ()
    out: list[ExperimentVariant] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        vid = str(entry.get("id") or "").strip()
        if not vid:
            continue
        out.append(ExperimentVariant(id=vid, label=str(entry.get("label") or "")))
    return tuple(out)


def _row_to_experiment(row: ExperimentRow) -> Experiment:
    return Experiment(
        id=row.id,
        name=row.name or "",
        description=row.description or "",
        variants=_variants_from_json(row.variants_json),
        salt=row.salt or "",
        active=bool(row.active),
        created_at=_ensure_utc(row.created_at),
    )


def _row_to_assignment(row: ExperimentAssignmentRow) -> ExperimentAssignment:
    return ExperimentAssignment(
        experiment_id=row.experiment_id,
        character_id=row.character_id,
        operator_id=row.operator_id,
        variant_id=row.variant_id,
        assigned_at=_ensure_utc(row.assigned_at),
    )


class SAExperimentRepository(ExperimentRepositoryPort):
    def __init__(self, session_factory: sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def add(self, experiment: Experiment) -> None:
        async with self._session_factory() as session:
            row = ExperimentRow(
                id=experiment.id,
                name=experiment.name,
                description=experiment.description,
                variants_json=_variants_to_json(experiment.variants),
                salt=experiment.salt,
                active=experiment.active,
                created_at=experiment.created_at,
            )
            session.add(row)
            await session.commit()

    async def get(self, experiment_id: str) -> Experiment | None:
        async with self._session_factory() as session:
            result = await session.execute(
                select(ExperimentRow).where(ExperimentRow.id == experiment_id),
            )
            row = result.scalar_one_or_none()
            return _row_to_experiment(row) if row else None

    async def list_active(self) -> list[Experiment]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(ExperimentRow).where(ExperimentRow.active.is_(True)),
            )
            return [_row_to_experiment(r) for r in result.scalars().all()]

    async def list_all(self) -> list[Experiment]:
        async with self._session_factory() as session:
            result = await session.execute(select(ExperimentRow))
            return [_row_to_experiment(r) for r in result.scalars().all()]

    async def set_active(self, experiment_id: str, *, active: bool) -> bool:
        async with self._session_factory() as session:
            result = await session.execute(
                update(ExperimentRow)
                .where(ExperimentRow.id == experiment_id)
                .values(active=active),
            )
            await session.commit()
            return (result.rowcount or 0) > 0


class SAExperimentAssignmentRepository(ExperimentAssignmentRepositoryPort):
    def __init__(self, session_factory: sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get(
        self,
        *,
        experiment_id: str,
        character_id: str,
        operator_id: str,
    ) -> ExperimentAssignment | None:
        async with self._session_factory() as session:
            result = await session.execute(
                select(ExperimentAssignmentRow).where(
                    ExperimentAssignmentRow.experiment_id == experiment_id,
                    ExperimentAssignmentRow.character_id == character_id,
                    ExperimentAssignmentRow.operator_id == operator_id,
                ),
            )
            row = result.scalar_one_or_none()
            return _row_to_assignment(row) if row else None

    async def upsert(self, assignment: ExperimentAssignment) -> None:
        now = _ensure_utc(assignment.assigned_at)
        async with self._session_factory() as session:
            dialect = session.bind.dialect.name if session.bind else ""
            values = {
                "experiment_id": assignment.experiment_id,
                "character_id": assignment.character_id,
                "operator_id": assignment.operator_id,
                "variant_id": assignment.variant_id,
                "assigned_at": now,
            }
            if dialect == "postgresql":
                stmt = pg_insert(ExperimentAssignmentRow).values(**values)
                stmt = stmt.on_conflict_do_update(
                    index_elements=[
                        ExperimentAssignmentRow.experiment_id,
                        ExperimentAssignmentRow.character_id,
                        ExperimentAssignmentRow.operator_id,
                    ],
                    set_={"variant_id": values["variant_id"], "assigned_at": now},
                )
            else:
                stmt = sqlite_insert(ExperimentAssignmentRow).values(**values)
                stmt = stmt.on_conflict_do_update(
                    index_elements=[
                        ExperimentAssignmentRow.experiment_id,
                        ExperimentAssignmentRow.character_id,
                        ExperimentAssignmentRow.operator_id,
                    ],
                    set_={"variant_id": values["variant_id"], "assigned_at": now},
                )
            await session.execute(stmt)
            await session.commit()

    async def list_for_experiment(
        self, experiment_id: str,
    ) -> list[ExperimentAssignment]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(ExperimentAssignmentRow).where(
                    ExperimentAssignmentRow.experiment_id == experiment_id,
                ),
            )
            return [_row_to_assignment(r) for r in result.scalars().all()]
