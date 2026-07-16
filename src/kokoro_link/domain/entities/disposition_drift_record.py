"""Audit row for one ``CharacterDisposition`` band shift (HUMANIZATION_ROADMAP §3.1).

The drift judge runs during the dream pass once every cooldown window
(default 30 days per dimension). When it decides one dimension's band
should nudge, we record a single ``DispositionDriftRecord`` row alongside
applying the change to the character. The row is the source of truth for
the audit timeline; the character's flat ``disposition_json`` column is
just the current snapshot derived from history.

Why a record and not just a column delta:
- The "人格演化軌跡" admin view (§3.1 frontend deliverable) reads this
  table directly; rendering the timeline from column-only state would
  require event-source reconstruction we don't otherwise need.
- ``evidence_quote`` is the anti-hallucination guard — same convention
  as persona extraction and self-reflection. The judge must cite a
  verbatim moment from the 30-day window, or the drift is rejected.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Final
from uuid import uuid4


_VALID_DIMENSIONS: Final = frozenset({
    "self_centeredness",
    "candor",
    "sharing_drive",
    "associativeness",
})
_VALID_BANDS: Final = frozenset({"low", "medium", "high"})


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class DispositionDriftRecord:
    id: str
    character_id: str
    dimension: str
    from_band: str
    to_band: str
    reason: str
    evidence_quote: str
    decided_at: datetime = field(default_factory=_utcnow)

    def __post_init__(self) -> None:
        if not self.character_id.strip():
            raise ValueError("DispositionDriftRecord.character_id must be non-empty")
        if self.dimension not in _VALID_DIMENSIONS:
            raise ValueError(
                f"DispositionDriftRecord.dimension must be one of "
                f"{sorted(_VALID_DIMENSIONS)}, got {self.dimension!r}",
            )
        if self.from_band not in _VALID_BANDS or self.to_band not in _VALID_BANDS:
            raise ValueError(
                f"DispositionDriftRecord bands must be one of "
                f"{sorted(_VALID_BANDS)}",
            )
        if self.from_band == self.to_band:
            raise ValueError(
                "DispositionDriftRecord must record an actual shift, "
                "got identical from/to bands",
            )

    @classmethod
    def new(
        cls,
        *,
        character_id: str,
        dimension: str,
        from_band: str,
        to_band: str,
        reason: str,
        evidence_quote: str = "",
        now: datetime | None = None,
    ) -> "DispositionDriftRecord":
        return cls(
            id=str(uuid4()),
            character_id=character_id.strip(),
            dimension=dimension,
            from_band=from_band,
            to_band=to_band,
            reason=reason.strip(),
            evidence_quote=evidence_quote.strip(),
            decided_at=now or _utcnow(),
        )
