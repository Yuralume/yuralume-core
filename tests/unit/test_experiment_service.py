"""Unit tests for :class:`ExperimentService` (HUMANIZATION_ROADMAP §4.6)."""

from __future__ import annotations

import pytest

from kokoro_link.application.services.experiment_service import ExperimentService
from kokoro_link.domain.entities.experiment import Experiment, ExperimentVariant
from kokoro_link.infrastructure.repositories.in_memory_experiments import (
    InMemoryExperimentAssignmentRepository,
    InMemoryExperimentRepository,
)


def _service() -> ExperimentService:
    return ExperimentService(
        experiment_repository=InMemoryExperimentRepository(),
        assignment_repository=InMemoryExperimentAssignmentRepository(),
    )


@pytest.mark.asyncio
async def test_create_returns_experiment_with_two_variants() -> None:
    svc = _service()
    exp = await svc.create_experiment(
        name="long-vs-short opener",
        description="compare two prompt openers",
        variant_ids=["control", "treatment"],
    )
    assert len(exp.variants) == 2
    assert exp.active
    assert exp.salt  # non-empty default salt


@pytest.mark.asyncio
async def test_assign_returns_sticky_variant() -> None:
    svc = _service()
    exp = await svc.create_experiment(
        name="x", description="", variant_ids=["a", "b"], salt="seed",
    )
    first = await svc.assign_variant(
        experiment_id=exp.id, character_id="char-1", operator_id="op-1",
    )
    second = await svc.assign_variant(
        experiment_id=exp.id, character_id="char-1", operator_id="op-1",
    )
    assert first is not None and second is not None
    assert first.id == second.id


@pytest.mark.asyncio
async def test_assign_returns_none_when_inactive() -> None:
    svc = _service()
    exp = await svc.create_experiment(
        name="x", description="", variant_ids=["a", "b"],
    )
    await svc.set_active(exp.id, active=False)
    result = await svc.assign_variant(
        experiment_id=exp.id, character_id="c", operator_id="o",
    )
    assert result is None


@pytest.mark.asyncio
async def test_different_pairs_spread_across_variants() -> None:
    """Sticky hashing should distribute many pairs across both variants —
    not strictly 50/50, but at least one assignment per variant in a large
    enough sample."""
    svc = _service()
    exp = await svc.create_experiment(
        name="x", description="", variant_ids=["a", "b"], salt="seed",
    )
    seen: set[str] = set()
    for i in range(200):
        v = await svc.assign_variant(
            experiment_id=exp.id, character_id=f"char-{i}", operator_id="op",
        )
        assert v is not None
        seen.add(v.id)
    assert seen == {"a", "b"}


@pytest.mark.asyncio
async def test_report_counts_assignments() -> None:
    svc = _service()
    exp = await svc.create_experiment(
        name="x", description="", variant_ids=["a", "b"], salt="seed",
    )
    for i in range(10):
        await svc.assign_variant(
            experiment_id=exp.id, character_id=f"c{i}", operator_id="op",
        )
    report = await svc.compile_report(exp.id)
    assert report is not None
    total = sum(b.assignment_count for b in report.buckets)
    assert total == 10


@pytest.mark.asyncio
async def test_create_experiment_rejects_single_variant() -> None:
    svc = _service()
    with pytest.raises(ValueError):
        await svc.create_experiment(
            name="x", description="", variant_ids=["only"],
        )


@pytest.mark.asyncio
async def test_create_experiment_rejects_duplicate_variant_ids() -> None:
    svc = _service()
    with pytest.raises(ValueError):
        await svc.create_experiment(
            name="x", description="", variant_ids=["a", "a"],
        )


def test_experiment_assign_is_deterministic_across_instances() -> None:
    """Different ``Experiment`` instances with the same id+salt must
    assign the same pair to the same variant; the hash is the sticky
    bucket — not a process-local random."""
    exp1 = Experiment(
        id="exp-1",
        name="x",
        description="",
        variants=(ExperimentVariant(id="a"), ExperimentVariant(id="b")),
        salt="seed",
    )
    exp2 = Experiment(
        id="exp-1",
        name="x",
        description="",
        variants=(ExperimentVariant(id="a"), ExperimentVariant(id="b")),
        salt="seed",
    )
    pair = ("char-7", "op-3")
    assert exp1.assign(character_id=pair[0], operator_id=pair[1]).id == \
        exp2.assign(character_id=pair[0], operator_id=pair[1]).id
