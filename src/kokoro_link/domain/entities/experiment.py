"""A/B experiment + sticky bucket entities (HUMANIZATION_ROADMAP §4.6).

Owner decision (2026-05-21): A/B framework collects structured results
per bucket — sample chats, subsystem-health slices, fixture judge scores
— and a manual operator-triggered batch job hands them to a high-tier
LLM for a written comparison report. We deliberately **do not**
auto-decide winners or auto-rebalance traffic.

Two entities:

* :class:`Experiment` — the campaign: id, variant ids, optional
  active-window timestamps, salt for deterministic hashing.
* :class:`ExperimentAssignment` — one row per (experiment, character,
  operator) pair recording which variant was picked. Sticky because the
  assignment is derived from a hash, not a counter.

Why ``(character_id, operator_id)`` granularity instead of just
``operator_id``: a single operator routinely runs multiple characters;
treating them as one unit would homogenise the experiment surface and
miss "character-X works better on variant A while character-Y prefers
variant B".
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Final
from uuid import uuid4


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class ExperimentVariant:
    """One arm of an experiment. ``id`` must be unique within the experiment."""

    id: str
    label: str = ""
    """Operator-facing short description (~40 chars)."""

    def __post_init__(self) -> None:
        cleaned = (self.id or "").strip()
        if not cleaned:
            raise ValueError("ExperimentVariant.id must be non-empty")
        object.__setattr__(self, "id", cleaned)
        object.__setattr__(self, "label", (self.label or "").strip()[:80])


@dataclass(frozen=True, slots=True)
class Experiment:
    id: str
    name: str
    description: str
    variants: tuple[ExperimentVariant, ...]
    salt: str
    active: bool = True
    created_at: datetime = field(default_factory=_utcnow)

    def __post_init__(self) -> None:
        if len(self.variants) < 2:
            raise ValueError("Experiment must have at least two variants")
        ids = {v.id for v in self.variants}
        if len(ids) != len(self.variants):
            raise ValueError("Experiment variant ids must be unique")
        object.__setattr__(self, "name", (self.name or "").strip()[:120])
        object.__setattr__(self, "description", (self.description or "").strip()[:480])
        object.__setattr__(self, "salt", (self.salt or "").strip())

    def assign(self, *, character_id: str, operator_id: str) -> ExperimentVariant:
        """Deterministic hash → variant. Same pair always lands in same bucket."""
        digest = hashlib.sha256(
            f"{self.id}:{self.salt}:{character_id}:{operator_id}".encode("utf-8"),
        ).digest()
        idx = int.from_bytes(digest[:8], "big") % len(self.variants)
        return self.variants[idx]

    @classmethod
    def new(
        cls,
        *,
        name: str,
        description: str,
        variant_ids: list[str],
        salt: str | None = None,
    ) -> "Experiment":
        return cls(
            id=str(uuid4()),
            name=name,
            description=description,
            variants=tuple(
                ExperimentVariant(id=v, label=v) for v in variant_ids
            ),
            salt=salt or str(uuid4())[:8],
            active=True,
            created_at=_utcnow(),
        )


@dataclass(frozen=True, slots=True)
class ExperimentAssignment:
    """Recorded (experiment, pair) → variant assignment.

    Persisted so we can: (a) ensure stickiness across process restarts
    even if the hash function ever changes, and (b) query "how many
    pairs landed in each variant" without re-computing every hash.
    """

    experiment_id: str
    character_id: str
    operator_id: str
    variant_id: str
    assigned_at: datetime = field(default_factory=_utcnow)


VARIANT_CONTROL: Final = "control"
VARIANT_TREATMENT: Final = "treatment"
