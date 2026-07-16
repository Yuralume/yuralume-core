"""Persona consolidator port — the "dream job" brain.

Periodically (during quiet hours) inspects pending candidates plus
existing confirmed fields and decides which to promote, merge,
supersede, decay, reject, or infer. Returns an action plan; the
service applies it inside a transaction so partial failure doesn't
leave the persona inconsistent.

Why a separate port from the extractor:

- Different temperature / model preference (consolidation benefits
  from a stronger reasoner; extraction is a tight observe-only task).
- Different inputs — extractor sees a single turn; consolidator sees
  the whole staging buffer plus current confirmed state.
- Failure semantics differ: a bad extraction batch drops on the
  floor, but a bad consolidation result needs to leave staging
  untouched so the next pass can retry.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from kokoro_link.domain.entities.operator_persona import OperatorPersona
from kokoro_link.domain.value_objects.profile_field import (
    CandidateField,
    ProfileField,
)


# ---- Action records ----------------------------------------------------------
#
# The consolidator returns a structured plan rather than mutating the
# persona directly. This keeps the port pure-functional (easier to
# test, easier to stub) and lets the service decide how to apply each
# action (e.g. failure tolerance, audit logging).


@dataclass(frozen=True, slots=True)
class PromoteAction:
    """Promote a pending candidate to a confirmed field."""

    candidate_id: str
    field_key: str
    layer: int
    value: str
    new_confidence: float
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class MergeAction:
    """Collapse several pending candidates into one confirmed field.

    The candidates listed in ``candidate_ids`` are all marked
    ``promoted`` (so they leave the staging queue) and one combined
    confirmed row is written.
    """

    candidate_ids: tuple[str, ...]
    field_key: str
    layer: int
    value: str
    new_confidence: float
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class SupersedeAction:
    """Replace an existing confirmed field with a new value.

    The old field is marked ``superseded`` (kept for audit), and a new
    confirmed field is written. Layer 1 requires ≥2 candidates as
    backing evidence; the consolidator enforces this itself.
    """

    superseded_field_id: str
    candidate_ids: tuple[str, ...]
    field_key: str
    layer: int
    new_value: str
    new_confidence: float
    reason: str


@dataclass(frozen=True, slots=True)
class RejectAction:
    """Drop a candidate as a hallucination or contradiction."""

    candidate_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class DecayAction:
    """Lower confidence on a stale confirmed field. If the new
    confidence falls below the layer's injection threshold, the prompt
    builder will naturally stop rendering it; if it falls past
    ``persona_stale_after_days`` the service marks it ``stale``.
    """

    field_id: str
    new_confidence: float
    reason: str


@dataclass(frozen=True, slots=True)
class InferAction:
    """Synthesise a new field from existing fields (cross-field
    inference). Source is fixed to ``dream_inference`` so the prompt
    builder can render with softer wording. Confidence MUST be ≤ 0.6
    — inferred facts are never as strong as observed ones."""

    field_key: str
    layer: int
    value: str
    new_confidence: float
    reason: str
    supporting_field_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ConsolidationResult:
    promotions: list[PromoteAction] = field(default_factory=list)
    merges: list[MergeAction] = field(default_factory=list)
    supersedes: list[SupersedeAction] = field(default_factory=list)
    rejections: list[RejectAction] = field(default_factory=list)
    decays: list[DecayAction] = field(default_factory=list)
    inferences: list[InferAction] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not (
            self.promotions
            or self.merges
            or self.supersedes
            or self.rejections
            or self.decays
            or self.inferences
        )


class PersonaConsolidatorPort(Protocol):
    async def consolidate(
        self,
        *,
        persona: OperatorPersona,
        pending: list[CandidateField],
        decay_candidates: list[ProfileField],
    ) -> ConsolidationResult:
        """Inspect staging + existing confirmed state and return an
        action plan.

        Failure modes — implementation MUST NOT raise. On any internal
        error return an empty :class:`ConsolidationResult` so the
        service falls through to the next tick.
        """
