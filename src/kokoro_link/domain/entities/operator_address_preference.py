"""Operator address preference entity (HUMANIZATION_ROADMAP §4.2).

One row per ``(character_id, operator_id)``. Captures the **observed**
register / address style the operator uses when talking to this
character:

- ``salutation`` — how the operator addresses the character (暱稱 /
  你 / 妳 / fullname / etc.). Free-form short string.
- ``formality_level`` — ``low`` / ``medium`` / ``high``. 質性 band 不
  暴露數值。
- ``response_length_pref`` — ``short`` / ``medium`` / ``long``.

Owner decision (2026-05-21): the **observed** preference **overrides**
the §3.6 ``operator_pace_preference`` explicit setting. Reason: the
user usually only edits the explicit knob once and forgets; auto-
observation lets the character evolve with the relationship. Both
signals are still surfaced as facts to the LLM; the priority rule
lives in the prompt builder, not in the entity.

Why a separate entity vs. extending ``OperatorPersona``: the persona
table is the five-layer relationship model and follows a different
mutation cadence (dream pass with confidence decay). Address
preference is a single observation row updated by an extractor and
read by every prompt — keeping it separate avoids forcing the prompt
path through the persona staging buffer.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Final


_VALID_FORMALITY: Final = frozenset({"low", "medium", "high"})
_VALID_LENGTH: Final = frozenset({"short", "medium", "long"})

FORMALITY_DEFAULT = "medium"
LENGTH_DEFAULT = "medium"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _normalise_band(raw: object, allowed: frozenset[str], default: str) -> str:
    if raw is None:
        return default
    if not isinstance(raw, str):
        return default
    cleaned = raw.strip().lower()
    if not cleaned:
        return default
    if cleaned not in allowed:
        return default
    return cleaned


@dataclass(frozen=True, slots=True)
class OperatorAddressPreference:
    character_id: str
    operator_id: str
    salutation: str = ""
    formality_level: str = FORMALITY_DEFAULT
    response_length_pref: str = LENGTH_DEFAULT
    evidence_quote: str = ""
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "formality_level",
            _normalise_band(self.formality_level, _VALID_FORMALITY, FORMALITY_DEFAULT),
        )
        object.__setattr__(
            self,
            "response_length_pref",
            _normalise_band(self.response_length_pref, _VALID_LENGTH, LENGTH_DEFAULT),
        )
        # Salutation is free-form; just strip + cap to avoid prompt
        # bloat from an accidentally-pasted paragraph.
        salutation = (self.salutation or "").strip()
        object.__setattr__(self, "salutation", salutation[:64])
        evidence = (self.evidence_quote or "").strip()
        object.__setattr__(self, "evidence_quote", evidence[:240])

    @property
    def is_empty(self) -> bool:
        """No observation worth surfacing — empty salutation + default bands."""
        return (
            not self.salutation
            and self.formality_level == FORMALITY_DEFAULT
            and self.response_length_pref == LENGTH_DEFAULT
        )

    def with_updates(
        self,
        *,
        salutation: str | None = None,
        formality_level: str | None = None,
        response_length_pref: str | None = None,
        evidence_quote: str | None = None,
        updated_at: datetime | None = None,
    ) -> "OperatorAddressPreference":
        return replace(
            self,
            salutation=self.salutation if salutation is None else salutation,
            formality_level=(
                self.formality_level if formality_level is None else formality_level
            ),
            response_length_pref=(
                self.response_length_pref
                if response_length_pref is None
                else response_length_pref
            ),
            evidence_quote=(
                self.evidence_quote if evidence_quote is None else evidence_quote
            ),
            updated_at=updated_at or _utcnow(),
        )

    @classmethod
    def empty(
        cls, *, character_id: str, operator_id: str,
    ) -> "OperatorAddressPreference":
        return cls(character_id=character_id, operator_id=operator_id)
