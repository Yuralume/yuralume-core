"""Bidirectional address resolver (HUMANIZATION_ROADMAP §4.2).

Collapses the scattered, un-prioritised name sources into a single
resolved term per direction. Pure functions: every input is passed in
by the caller (which already fetches seed / persona / profile /
preference), so the resolver does no I/O and stays trivially testable.

Per-character isolation is structural: the player direction *reads*
per-(character, operator) sources to override the global profile, but
never writes back to it — keeping one character's local nickname out of
every other character's prompt (mirrors the deliberate no-op in
``persona_dream_service._maybe_sync_operator_display_name``).

Direction A — how the character addresses the **player**::

    seed.user_address_name              (explicit per-char intent)
  > persona layer1 name / nickname      (learned, confidence-gated)
  > profile.display_name                (global platform label)
  > fallback (no real name → caller keeps legacy wording)

Direction B — how the **player** addresses the character::

    seed.character_address_name         (explicit per-char intent)
  > address_preference.salutation       (observed)
  > character.name                      (the character's own name)
  > fallback
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from kokoro_link.domain.entities.conversation import MessageContentMode
from kokoro_link.domain.entities.operator_profile import (
    DEFAULT_OPERATOR_DISPLAY_NAME,
)
from kokoro_link.domain.value_objects.resolved_address import (
    AddressProvenance,
    NEUTRAL_OTHER,
    ResolvedAddress,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kokoro_link.domain.entities.character import Character
    from kokoro_link.domain.entities.character_operator_relationship_seed import (
        CharacterOperatorRelationshipSeed,
    )
    from kokoro_link.domain.entities.operator_address_preference import (
        OperatorAddressPreference,
    )
    from kokoro_link.domain.entities.operator_persona import OperatorPersona
    from kokoro_link.domain.entities.operator_profile import OperatorProfile
    from kokoro_link.domain.value_objects.profile_field import ProfileField


_PERSONA_NAME_MIN_CONFIDENCE: Final = 0.7
"""Layer-1 inject threshold, matching ``OperatorPersonaService`` /
projection. A learned name below this never becomes an address term."""

_MAX_ALIASES: Final = 6
"""Cap recognised alternates so a long alias history can't bloat prompts."""


def resolve_player_address(
    *,
    seed: "CharacterOperatorRelationshipSeed | None" = None,
    persona: "OperatorPersona | None" = None,
    profile: "OperatorProfile | None" = None,
) -> ResolvedAddress:
    """Resolve how the character should address the player (direction A)."""
    seed_name = _clean(getattr(seed, "user_address_name", None))
    # Use the raw display name unless it's still the 「操作者」 placeholder
    # (the "no real name yet" sentinel). Checked by value rather than via
    # ``has_real_name()`` so the resolver tolerates duck-typed callers.
    display_name = _clean(getattr(profile, "display_name", None))
    if display_name == DEFAULT_OPERATOR_DISPLAY_NAME:
        display_name = ""
    profile_aliases = tuple(getattr(profile, "aliases", ()) or ())

    candidates: list[tuple[str, AddressProvenance]] = []
    if seed_name:
        candidates.append((seed_name, AddressProvenance.EXPLICIT_SEED))
    # ``name`` outranks ``nickname``; whichever isn't primary becomes a
    # recognised alias so the same person is identifiable under either.
    for persona_name in _persona_name_candidates(persona):
        candidates.append((persona_name, AddressProvenance.LEARNED_PERSONA))
    if display_name:
        candidates.append((display_name, AddressProvenance.PLATFORM_PROFILE))

    return _build(candidates, extra_aliases=profile_aliases)


def resolve_character_address(
    *,
    seed: "CharacterOperatorRelationshipSeed | None" = None,
    preference: "OperatorAddressPreference | None" = None,
    character: "Character | None" = None,
) -> ResolvedAddress:
    """Resolve how the player addresses the character (direction B)."""
    seed_name = _clean(getattr(seed, "character_address_name", None))
    observed = _clean(getattr(preference, "salutation", None))
    character_name = _clean(getattr(character, "name", None))

    candidates: list[tuple[str, AddressProvenance]] = []
    if seed_name:
        candidates.append((seed_name, AddressProvenance.EXPLICIT_SEED))
    if observed:
        candidates.append((observed, AddressProvenance.OBSERVED_PREFERENCE))
    if character_name:
        candidates.append((character_name, AddressProvenance.CHARACTER_NAME))

    return _build(candidates)


def _build(
    candidates: list[tuple[str, AddressProvenance]],
    *,
    extra_aliases: tuple[str, ...] = (),
) -> ResolvedAddress:
    """Pick the highest-precedence candidate as primary; the remaining
    candidate values plus any explicit extra aliases become recognised
    alternates (the value object dedupes and drops the primary)."""
    if not candidates:
        return ResolvedAddress(
            primary=NEUTRAL_OTHER,
            provenance=AddressProvenance.FALLBACK,
        )
    primary, provenance = candidates[0]
    # Explicit alternates first (operator-declared aliases), then the
    # lower-precedence candidate names so memories under an older name
    # still resolve to the same person.
    aliases = (*extra_aliases, *(value for value, _ in candidates[1:]))
    return ResolvedAddress(
        primary=primary,
        aliases=aliases[:_MAX_ALIASES],
        provenance=provenance,
    )


def _persona_name_candidates(persona: "OperatorPersona | None") -> list[str]:
    """Learned names for the player in precedence order: ``name`` then
    ``nickname``, each gated on the layer-1 confidence threshold and
    excluding NSFW-sourced facts."""
    if persona is None:
        return []
    names: list[str] = []
    for field_key in ("name", "nickname"):
        field = persona.layer1_identity.get(field_key)
        if field is None or not _persona_field_usable(field):
            continue
        value = _clean(field.value)
        if value:
            names.append(value)
    return names


def _persona_field_usable(field: "ProfileField") -> bool:
    if field.confidence < _PERSONA_NAME_MIN_CONFIDENCE:
        return False
    return field.content_mode is not MessageContentMode.NSFW


def _clean(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()
