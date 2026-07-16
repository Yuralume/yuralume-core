"""Heuristic memory decay.

Some memories earn their keep (high salience, still relevant, actively
recalled); others turn into noise over time (low salience, ancient, and
never touched again). This module prunes the second group with a
simple deterministic rule — no LLM call, no embeddings — so even a
headless environment can keep the pool from growing unbounded.

Rule (all three must hold):

    salience    < ``min_salience``         (default 0.25)
    age         > ``max_age``              (default 90 days)
    access_count == 0                      (never touched after write)

Rationale for AND-ing the three signals:

- Low salience alone would kill useful semantic facts that happen to
  score low because the extractor was conservative.
- Age alone would erase important long-standing relationship notes.
- Access count alone would erase anything the user hasn't circled back
  to — but that includes reliable background facts we just haven't had
  a reason to recall yet.

Combining all three targets the intersection: old + unimportant-looking
+ never re-read. That's the population most likely to be noise.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from kokoro_link.domain.entities.memory_item import MemoryItem


_DEFAULT_MIN_SALIENCE = 0.25
_DEFAULT_MAX_AGE_DAYS = 90.0


@dataclass(frozen=True, slots=True)
class DecayPolicy:
    min_salience: float = _DEFAULT_MIN_SALIENCE
    max_age_days: float = _DEFAULT_MAX_AGE_DAYS
    require_never_accessed: bool = True


@dataclass(frozen=True, slots=True)
class DecayPlan:
    """What would be removed — produced by ``plan`` for dry-runs and
    also consumed by ``apply`` so the two paths agree on selection.
    """

    character_id: str
    item_ids: list[str] = field(default_factory=list)
    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def count(self) -> int:
        return len(self.item_ids)


def plan_decay(
    items: Iterable[MemoryItem],
    *,
    character_id: str,
    policy: DecayPolicy | None = None,
    now: datetime | None = None,
) -> DecayPlan:
    """Return the set of memories that match the decay rule."""
    active = policy or DecayPolicy()
    reference_now = now or datetime.now(timezone.utc)
    cutoff = reference_now - timedelta(days=active.max_age_days)

    targets: list[str] = []
    for item in items:
        created_at = item.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        if item.salience >= active.min_salience:
            continue
        if created_at > cutoff:
            continue
        if active.require_never_accessed and item.access_count > 0:
            continue
        targets.append(item.id)
    return DecayPlan(
        character_id=character_id,
        item_ids=targets,
        now=reference_now,
    )
