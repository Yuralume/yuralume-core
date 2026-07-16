"""Repository ports for :class:`Experiment` and :class:`ExperimentAssignment` (§4.6)."""

from __future__ import annotations

from typing import Protocol

from kokoro_link.domain.entities.experiment import (
    Experiment,
    ExperimentAssignment,
)


class ExperimentRepositoryPort(Protocol):
    async def add(self, experiment: Experiment) -> None:
        ...

    async def get(self, experiment_id: str) -> Experiment | None:
        ...

    async def list_active(self) -> list[Experiment]:
        ...

    async def list_all(self) -> list[Experiment]:
        ...

    async def set_active(self, experiment_id: str, *, active: bool) -> bool:
        ...


class ExperimentAssignmentRepositoryPort(Protocol):
    async def get(
        self,
        *,
        experiment_id: str,
        character_id: str,
        operator_id: str,
    ) -> ExperimentAssignment | None:
        ...

    async def upsert(self, assignment: ExperimentAssignment) -> None:
        ...

    async def list_for_experiment(
        self, experiment_id: str,
    ) -> list[ExperimentAssignment]:
        ...
