"""Long-window behavioural pattern entity (HUMANIZATION_ROADMAP §3.3).

A pure observation row — the dream pass (or a tick-level extractor)
records statistically recurring shapes of the character's recent life:

- ``recurring_activity``: "this character tends to do X on Y day around
  Z time" — derived from ``DailySchedule`` history.
- ``time_preference``: "this character is usually active in the
  late evening / sleeps in" — derived from schedule cumulative load.
- ``phrase_habit``: "this character keeps reusing 「沒事啦」 / "sigh"" —
  derived from the character's own recent assistant lines.

Why store these instead of computing on demand:

- The prompt is hot path; running schedule-history queries every chat
  turn would cost real latency.
- Storing the entity lets the dashboard surface them and dream-time
  jobs reason about decay.
- ``observed_count`` carries the statistical weight without exposing
  raw histograms to the LLM (LLM-first 紅線: facts not knobs).

Pure-Python entity, no SQLAlchemy / Pydantic. Persistence lives in
``infrastructure/persistence/sa_behavioral_pattern_repository.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Final
from uuid import uuid4


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


KIND_RECURRING_ACTIVITY: Final = "recurring_activity"
KIND_PHRASE_HABIT: Final = "phrase_habit"
KIND_TIME_PREFERENCE: Final = "time_preference"

_VALID_KINDS: Final = frozenset({
    KIND_RECURRING_ACTIVITY,
    KIND_PHRASE_HABIT,
    KIND_TIME_PREFERENCE,
})


@dataclass(frozen=True, slots=True)
class BehavioralPattern:
    id: str
    character_id: str
    kind: str
    description: str
    """Free-text qualitative description. Render-ready for prompt
    injection ("週末上午常去咖啡廳" / "說話常加「沒事啦」結尾")."""
    observed_count: int
    """Statistical weight (e.g. # of weeks this recurrence was seen).
    Used by the prompt builder only to decide ordering / inclusion
    threshold, **not** to be echoed to the LLM verbatim. LLM sees
    only the qualitative description."""
    first_observed_at: datetime
    last_observed_at: datetime
    salience: float = 0.5
    """0–1 weighting that downstream injection can use to drop low-
    confidence patterns. Mirrors ``MemoryItem.salience`` so we have
    a consistent decay knob across observation surfaces."""

    def __post_init__(self) -> None:
        if not self.character_id.strip():
            raise ValueError("BehavioralPattern.character_id must be non-empty")
        if self.kind not in _VALID_KINDS:
            raise ValueError(
                f"BehavioralPattern.kind must be one of {sorted(_VALID_KINDS)}, "
                f"got {self.kind!r}",
            )
        if not self.description.strip():
            raise ValueError("BehavioralPattern.description must be non-empty")
        if self.observed_count < 1:
            raise ValueError("BehavioralPattern.observed_count must be >= 1")
        clamped = max(0.0, min(1.0, float(self.salience)))
        if clamped != self.salience:
            object.__setattr__(self, "salience", clamped)
        if self.last_observed_at < self.first_observed_at:
            raise ValueError(
                "BehavioralPattern.last_observed_at must be >= first_observed_at",
            )

    @classmethod
    def new(
        cls,
        *,
        character_id: str,
        kind: str,
        description: str,
        observed_count: int = 1,
        salience: float = 0.5,
        first_observed_at: datetime | None = None,
        last_observed_at: datetime | None = None,
    ) -> "BehavioralPattern":
        ref = _utcnow()
        # When only ``last_observed_at`` is supplied (a fixture / dream
        # pass scenario), default ``first_observed_at`` to it so we never
        # synthesise a ``first > last`` pair that would fail the
        # invariant. Symmetric: only ``first`` given → mirror to last.
        if first_observed_at is None and last_observed_at is None:
            first = ref
            last = ref
        elif first_observed_at is None:
            first = last_observed_at  # type: ignore[assignment]
            last = last_observed_at  # type: ignore[assignment]
        elif last_observed_at is None:
            first = first_observed_at
            last = first_observed_at
        else:
            first = first_observed_at
            last = last_observed_at
        return cls(
            id=str(uuid4()),
            character_id=character_id.strip(),
            kind=kind,
            description=description.strip(),
            observed_count=max(1, int(observed_count)),
            first_observed_at=first,
            last_observed_at=last,
            salience=salience,
        )

    def reinforced(
        self, *, now: datetime | None = None, salience: float | None = None,
    ) -> "BehavioralPattern":
        """Return a copy with bump counters — used when the extractor sees
        the same pattern again on a later pass."""
        return replace(
            self,
            observed_count=self.observed_count + 1,
            last_observed_at=now or _utcnow(),
            salience=salience if salience is not None else self.salience,
        )
