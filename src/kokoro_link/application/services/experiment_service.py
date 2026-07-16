"""Experiment + sticky-bucket service (HUMANIZATION_ROADMAP §4.6).

Owner decision (2026-05-21): the A/B framework collects structured
results per bucket but never auto-decides winners or auto-rebalances
traffic. The service therefore exposes:

1. ``assign_variant(...)`` — returns the variant for a ``(character,
   operator)`` pair, persisting the assignment row the first time so
   subsequent calls hit the DB cache (cheap + verifiable in the admin
   dashboard).
2. ``create_experiment(...)`` — admin-only; mints the experiment row.
3. ``compile_report(...)`` — structured per-variant snapshot consumed
   by the manual high-tier LLM analysis (§4.6 結構化結果收集 item).

The high-tier LLM analysis step itself is a separate explicit admin
trigger — see ``api/routes/experiments.py``. The service does not
auto-fire any LLM call.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from kokoro_link.contracts.experiment import (
    ExperimentAssignmentRepositoryPort,
    ExperimentRepositoryPort,
)
from kokoro_link.domain.entities.experiment import (
    Experiment,
    ExperimentAssignment,
    ExperimentVariant,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class VariantBucketSummary:
    variant_id: str
    label: str
    assignment_count: int


@dataclass(frozen=True, slots=True)
class ExperimentReport:
    experiment_id: str
    name: str
    description: str
    salt: str
    active: bool
    buckets: list[VariantBucketSummary]
    metadata: dict[str, Any] = field(default_factory=dict)
    """Free-form bag for high-tier LLM analysis input — subsystem-health slice,
    fixture-judge scores, sample turns. Populated by ``compile_report``
    callers; the service leaves the bag empty so producers can attach
    whatever the analyst needs without growing the dataclass."""


class ExperimentService:
    def __init__(
        self,
        *,
        experiment_repository: ExperimentRepositoryPort,
        assignment_repository: ExperimentAssignmentRepositoryPort,
    ) -> None:
        self._experiments = experiment_repository
        self._assignments = assignment_repository

    async def list_active(self) -> list[Experiment]:
        """Surface active experiments for the overlay service / dashboard."""
        return await self._experiments.list_active()

    async def create_experiment(
        self,
        *,
        name: str,
        description: str,
        variant_ids: list[str],
        salt: str | None = None,
    ) -> Experiment:
        experiment = Experiment.new(
            name=name,
            description=description,
            variant_ids=variant_ids,
            salt=salt,
        )
        await self._experiments.add(experiment)
        return experiment

    async def list_experiments(self) -> list[Experiment]:
        return await self._experiments.list_all()

    async def get_experiment(self, experiment_id: str) -> Experiment | None:
        return await self._experiments.get(experiment_id)

    async def set_active(
        self, experiment_id: str, *, active: bool,
    ) -> bool:
        return await self._experiments.set_active(experiment_id, active=active)

    async def assign_variant(
        self,
        *,
        experiment_id: str,
        character_id: str,
        operator_id: str,
    ) -> ExperimentVariant | None:
        experiment = await self._experiments.get(experiment_id)
        if experiment is None or not experiment.active:
            return None
        existing = await self._assignments.get(
            experiment_id=experiment_id,
            character_id=character_id,
            operator_id=operator_id,
        )
        if existing is not None:
            for v in experiment.variants:
                if v.id == existing.variant_id:
                    return v
            # Variant id no longer exists in the experiment definition —
            # fall through to a fresh deterministic assignment.
        variant = experiment.assign(
            character_id=character_id, operator_id=operator_id,
        )
        await self._assignments.upsert(
            ExperimentAssignment(
                experiment_id=experiment_id,
                character_id=character_id,
                operator_id=operator_id,
                variant_id=variant.id,
            ),
        )
        return variant

    async def compile_report(self, experiment_id: str) -> ExperimentReport | None:
        experiment = await self._experiments.get(experiment_id)
        if experiment is None:
            return None
        assignments = await self._assignments.list_for_experiment(experiment_id)
        counts: dict[str, int] = {v.id: 0 for v in experiment.variants}
        for a in assignments:
            if a.variant_id in counts:
                counts[a.variant_id] += 1
        buckets = [
            VariantBucketSummary(
                variant_id=v.id, label=v.label, assignment_count=counts[v.id],
            )
            for v in experiment.variants
        ]
        return ExperimentReport(
            experiment_id=experiment.id,
            name=experiment.name,
            description=experiment.description,
            salt=experiment.salt,
            active=experiment.active,
            buckets=buckets,
        )
