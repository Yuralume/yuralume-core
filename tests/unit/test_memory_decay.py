"""Tests for the heuristic decay planner."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from kokoro_link.domain.entities.memory_item import MemoryItem
from kokoro_link.domain.value_objects.memory_kind import MemoryKind
from kokoro_link.infrastructure.memory.decay import DecayPolicy, plan_decay


def _item(
    *,
    salience: float = 0.5,
    age_days: float = 0.0,
    access_count: int = 0,
) -> MemoryItem:
    created = datetime.now(timezone.utc) - timedelta(days=age_days)
    return MemoryItem(
        id=str(uuid4()),
        character_id="c1",
        conversation_id=None,
        kind=MemoryKind.SEMANTIC,
        content="memory",
        salience=salience,
        created_at=created,
        access_count=access_count,
    )


def test_removes_stale_low_salience_unaccessed() -> None:
    targets = [
        _item(salience=0.1, age_days=200, access_count=0),
        _item(salience=0.2, age_days=120, access_count=0),
    ]
    plan = plan_decay(targets, character_id="c1")
    assert plan.count == 2


def test_keeps_recent_even_if_low_salience() -> None:
    items = [_item(salience=0.1, age_days=5, access_count=0)]
    plan = plan_decay(items, character_id="c1")
    assert plan.count == 0


def test_keeps_high_salience_even_if_old() -> None:
    items = [_item(salience=0.9, age_days=365, access_count=0)]
    plan = plan_decay(items, character_id="c1")
    assert plan.count == 0


def test_keeps_accessed_even_if_old_and_low() -> None:
    items = [_item(salience=0.1, age_days=365, access_count=3)]
    plan = plan_decay(items, character_id="c1")
    assert plan.count == 0


def test_policy_override_threshold_salience() -> None:
    items = [_item(salience=0.4, age_days=200, access_count=0)]
    plan = plan_decay(
        items, character_id="c1",
        policy=DecayPolicy(min_salience=0.5),
    )
    assert plan.count == 1


def test_policy_override_max_age() -> None:
    items = [_item(salience=0.1, age_days=40, access_count=0)]
    plan = plan_decay(
        items, character_id="c1",
        policy=DecayPolicy(max_age_days=30),
    )
    assert plan.count == 1


def test_require_never_accessed_false_ignores_usage() -> None:
    items = [_item(salience=0.1, age_days=200, access_count=5)]
    plan = plan_decay(
        items, character_id="c1",
        policy=DecayPolicy(require_never_accessed=False),
    )
    assert plan.count == 1
