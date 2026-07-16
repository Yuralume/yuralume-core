"""In-memory experiment + assignment stores (HUMANIZATION_ROADMAP §4.6)."""

from __future__ import annotations

from kokoro_link.contracts.experiment import (
    ExperimentAssignmentRepositoryPort,
    ExperimentRepositoryPort,
)
from kokoro_link.domain.entities.experiment import (
    Experiment,
    ExperimentAssignment,
)


class InMemoryExperimentRepository(ExperimentRepositoryPort):
    def __init__(self) -> None:
        self._rows: dict[str, Experiment] = {}

    async def add(self, experiment: Experiment) -> None:
        self._rows[experiment.id] = experiment

    async def get(self, experiment_id: str) -> Experiment | None:
        return self._rows.get(experiment_id)

    async def list_active(self) -> list[Experiment]:
        return [e for e in self._rows.values() if e.active]

    async def list_all(self) -> list[Experiment]:
        return list(self._rows.values())

    async def set_active(self, experiment_id: str, *, active: bool) -> bool:
        existing = self._rows.get(experiment_id)
        if existing is None:
            return False
        from dataclasses import replace
        self._rows[experiment_id] = replace(existing, active=active)
        return True


class InMemoryExperimentAssignmentRepository(
    ExperimentAssignmentRepositoryPort,
):
    def __init__(self) -> None:
        self._rows: dict[tuple[str, str, str], ExperimentAssignment] = {}

    async def get(
        self, *, experiment_id: str, character_id: str, operator_id: str,
    ) -> ExperimentAssignment | None:
        return self._rows.get((experiment_id, character_id, operator_id))

    async def upsert(self, assignment: ExperimentAssignment) -> None:
        self._rows[
            (assignment.experiment_id, assignment.character_id, assignment.operator_id)
        ] = assignment

    async def list_for_experiment(
        self, experiment_id: str,
    ) -> list[ExperimentAssignment]:
        return [
            a for a in self._rows.values() if a.experiment_id == experiment_id
        ]
