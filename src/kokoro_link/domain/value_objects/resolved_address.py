"""ResolvedAddress — the single address term a resolver picks for one
direction of the operator <-> character relationship.

Two directions exist and never share a value:

- **player** direction: how the character should address the operator
  (resolved from the per-character relationship seed, the learned
  persona name, or the global profile display name).
- **character** direction: how the operator addresses the character
  (resolved from the seed or the observed address preference).

The resolver returns the highest-precedence ``primary`` plus the
remaining candidate names as ``aliases`` so a prompt/memory consumer
can still recognise the same person under an older or alternate name
without the resolver having to rewrite history.

``provenance`` lets a caller decide *how* to render: an explicit seed
value is authoritative, a fallback means "no real name yet — keep the
legacy neutral wording".
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


NEUTRAL_OTHER: str = "對方"
"""Neutral term used when no real name resolves in either direction.

Callers that must always emit a string (e.g. memory-extraction
instructions) use ``primary`` directly; callers that should render
*nothing* when there is no real name check :attr:`ResolvedAddress.is_fallback`
so the legacy 「使用者」 wording stays natural."""


class AddressProvenance(str, Enum):
    """Where the resolved primary term came from, in precedence order."""

    EXPLICIT_SEED = "explicit_seed"
    OBSERVED_PREFERENCE = "observed_preference"
    LEARNED_PERSONA = "learned_persona"
    PLATFORM_PROFILE = "platform_profile"
    CHARACTER_NAME = "character_name"
    FALLBACK = "fallback"


@dataclass(frozen=True, slots=True)
class ResolvedAddress:
    """One direction's resolved address term plus recognised aliases."""

    primary: str
    aliases: tuple[str, ...] = ()
    provenance: AddressProvenance = AddressProvenance.FALLBACK

    def __post_init__(self) -> None:
        primary = (self.primary or "").strip()
        object.__setattr__(self, "primary", primary or NEUTRAL_OTHER)
        # Aliases are recognised alternates — strip, drop empties, drop
        # anything equal to the primary, and dedupe preserving order.
        seen: set[str] = {self.primary}
        cleaned: list[str] = []
        for alias in self.aliases:
            text = (alias or "").strip()
            if not text or text in seen:
                continue
            seen.add(text)
            cleaned.append(text)
        object.__setattr__(self, "aliases", tuple(cleaned))

    @property
    def is_fallback(self) -> bool:
        """``True`` when no real name resolved — caller should keep the
        legacy neutral wording rather than emit a made-up name."""
        return self.provenance is AddressProvenance.FALLBACK

    @property
    def has_aliases(self) -> bool:
        return bool(self.aliases)
