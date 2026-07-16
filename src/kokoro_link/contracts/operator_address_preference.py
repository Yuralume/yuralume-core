"""Repository + observer ports for ``OperatorAddressPreference`` (§4.2)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from kokoro_link.domain.entities.operator_address_preference import (
    OperatorAddressPreference,
)


class OperatorAddressPreferenceRepositoryPort(Protocol):
    async def get(
        self, *, character_id: str, operator_id: str,
    ) -> OperatorAddressPreference | None:
        """Return the persisted preference row or ``None`` when unset.

        Per-pair isolation: callers must not fall back to another pair's
        observation."""

    async def upsert(self, pref: OperatorAddressPreference) -> None:
        """Persist (insert or update) the preference for the pair."""


@dataclass(frozen=True, slots=True)
class AddressObservationCandidate:
    """One LLM-emitted candidate update.

    Empty / unknown fields collapse to ``None`` so the observer service
    can preserve prior values when the LLM has no new signal."""

    salutation: str | None = None
    formality_level: str | None = None
    response_length_pref: str | None = None
    evidence_quote: str = ""


class OperatorAddressObserverPort(Protocol):
    async def observe(
        self,
        *,
        character_id: str,
        operator_id: str,
        recent_user_messages: list[str],
    ) -> AddressObservationCandidate | None:
        """Return a candidate update, or ``None`` when no signal.

        The implementor may be an LLM (production) or a deterministic
        stub (tests / fake provider). Returning ``None`` means "leave
        the existing preference alone"."""
