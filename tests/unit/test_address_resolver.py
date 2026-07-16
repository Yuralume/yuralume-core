"""Unit tests for the bidirectional address resolver (pure domain service)."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from kokoro_link.domain.entities.character_operator_relationship_seed import (
    CharacterOperatorRelationshipSeed,
)
from kokoro_link.domain.entities.conversation import MessageContentMode
from kokoro_link.domain.entities.operator_address_preference import (
    OperatorAddressPreference,
)
from kokoro_link.domain.entities.operator_persona import OperatorPersona
from kokoro_link.domain.entities.operator_profile import OperatorProfile
from kokoro_link.domain.services.address_resolver import (
    resolve_character_address,
    resolve_player_address,
)
from kokoro_link.domain.value_objects.profile_field import EvidenceRef, ProfileField
from kokoro_link.domain.value_objects.resolved_address import (
    NEUTRAL_OTHER,
    AddressProvenance,
)


CHAR = "char-1"
OP = "op-1"


def _persona(*, name: str | None = None, nickname: str | None = None,
             confidence: float = 0.9,
             content_mode: MessageContentMode = MessageContentMode.NORMAL,
             ) -> OperatorPersona:
    layer1: dict[str, ProfileField] = {}
    for key, value in (("name", name), ("nickname", nickname)):
        if value is None:
            continue
        layer1[key] = ProfileField(
            field_key=key,
            layer=1,
            value=value,
            confidence=confidence,
            evidence_refs=(
                EvidenceRef(
                    turn_id="t1",
                    conversation_id="c1",
                    quote=value,
                    extracted_at=datetime.now(timezone.utc),
                ),
            ),
            last_updated=datetime.now(timezone.utc),
            update_count=1,
            source="extraction",
            content_mode=content_mode,
            character_id=CHAR,
        )
    return OperatorPersona(character_id=CHAR, operator_id=OP, layer1_identity=layer1)


def _seed(*, user_address_name: str = "", character_address_name: str = "",
          ) -> CharacterOperatorRelationshipSeed:
    return CharacterOperatorRelationshipSeed(
        character_id=CHAR,
        operator_id=OP,
        user_address_name=user_address_name,
        character_address_name=character_address_name,
    )


def _profile(display_name: str = "Alex",
             aliases: tuple[str, ...] = ()) -> OperatorProfile:
    return OperatorProfile(id=OP, display_name=display_name, aliases=aliases)


# --------------------------------------------------------------------------
# Direction A — how the character addresses the player
# --------------------------------------------------------------------------


def test_player_seed_wins_over_persona_and_profile() -> None:
    resolved = resolve_player_address(
        seed=_seed(user_address_name="阿力"),
        persona=_persona(name="艾力"),
        profile=_profile("alex_4821", aliases=("LEX",)),
    )
    assert resolved.primary == "阿力"
    assert resolved.provenance is AddressProvenance.EXPLICIT_SEED
    # lower-precedence names + explicit aliases are recognised alternates
    assert "艾力" in resolved.aliases
    assert "alex_4821" in resolved.aliases
    assert "LEX" in resolved.aliases


def test_player_persona_name_used_when_no_seed() -> None:
    resolved = resolve_player_address(
        persona=_persona(name="艾力"),
        profile=_profile("alex_4821"),
    )
    assert resolved.primary == "艾力"
    assert resolved.provenance is AddressProvenance.LEARNED_PERSONA
    assert "alex_4821" in resolved.aliases


def test_player_persona_name_preferred_over_nickname() -> None:
    resolved = resolve_player_address(persona=_persona(name="艾力", nickname="小丹"))
    assert resolved.primary == "艾力"
    assert "小丹" in resolved.aliases


def test_player_persona_nickname_used_when_no_name() -> None:
    resolved = resolve_player_address(
        persona=_persona(nickname="小丹"),
        profile=_profile("alex_4821"),
    )
    assert resolved.primary == "小丹"
    assert resolved.provenance is AddressProvenance.LEARNED_PERSONA


def test_player_low_confidence_persona_ignored() -> None:
    resolved = resolve_player_address(
        persona=_persona(name="艾力", confidence=0.69),
        profile=_profile("alex_4821"),
    )
    assert resolved.primary == "alex_4821"
    assert resolved.provenance is AddressProvenance.PLATFORM_PROFILE
    assert "艾力" not in resolved.aliases


def test_player_nsfw_persona_name_ignored() -> None:
    resolved = resolve_player_address(
        persona=_persona(name="艾力", content_mode=MessageContentMode.NSFW),
        profile=_profile("alex_4821"),
    )
    assert resolved.primary == "alex_4821"
    assert resolved.provenance is AddressProvenance.PLATFORM_PROFILE


def test_player_profile_display_name_used_when_no_seed_or_persona() -> None:
    resolved = resolve_player_address(profile=_profile("Alex", aliases=("D",)))
    assert resolved.primary == "Alex"
    assert resolved.provenance is AddressProvenance.PLATFORM_PROFILE
    assert resolved.aliases == ("D",)


def test_player_fallback_when_nothing_resolves() -> None:
    resolved = resolve_player_address()
    assert resolved.primary == NEUTRAL_OTHER
    assert resolved.is_fallback is True


def test_player_default_display_name_is_not_a_real_name() -> None:
    # The synthetic placeholder profile must fall through to fallback so
    # callers keep the legacy 「使用者」 wording.
    resolved = resolve_player_address(profile=OperatorProfile.default())
    assert resolved.is_fallback is True


# --------------------------------------------------------------------------
# Direction B — how the player addresses the character
# --------------------------------------------------------------------------


def test_character_seed_wins_over_salutation_and_name() -> None:
    resolved = resolve_character_address(
        seed=_seed(character_address_name="美緒姐"),
        preference=OperatorAddressPreference(
            character_id=CHAR, operator_id=OP, salutation="美緒",
        ),
        character=SimpleNamespace(name="美緒"),
    )
    assert resolved.primary == "美緒姐"
    assert resolved.provenance is AddressProvenance.EXPLICIT_SEED
    assert "美緒" in resolved.aliases


def test_character_observed_salutation_used_when_no_seed() -> None:
    resolved = resolve_character_address(
        preference=OperatorAddressPreference(
            character_id=CHAR, operator_id=OP, salutation="蓁蓁",
        ),
        character=SimpleNamespace(name="蓁蓁喵"),
    )
    assert resolved.primary == "蓁蓁"
    assert resolved.provenance is AddressProvenance.OBSERVED_PREFERENCE


def test_character_name_used_when_no_seed_or_preference() -> None:
    resolved = resolve_character_address(character=SimpleNamespace(name="美緒"))
    assert resolved.primary == "美緒"
    assert resolved.provenance is AddressProvenance.CHARACTER_NAME


def test_character_fallback_when_nothing() -> None:
    resolved = resolve_character_address()
    assert resolved.primary == NEUTRAL_OTHER
    assert resolved.is_fallback is True


def test_character_empty_salutation_skipped() -> None:
    resolved = resolve_character_address(
        preference=OperatorAddressPreference(
            character_id=CHAR, operator_id=OP, salutation="",
        ),
        character=SimpleNamespace(name="美緒"),
    )
    assert resolved.primary == "美緒"
    assert resolved.provenance is AddressProvenance.CHARACTER_NAME


# --------------------------------------------------------------------------
# ResolvedAddress value-object invariants
# --------------------------------------------------------------------------


def test_aliases_dedupe_and_exclude_primary() -> None:
    resolved = resolve_player_address(
        seed=_seed(user_address_name="阿力"),
        persona=_persona(name="阿力"),  # same as seed → must not appear in aliases
        profile=_profile("阿力", aliases=("阿力", "LEX")),
    )
    assert resolved.primary == "阿力"
    assert resolved.aliases == ("LEX",)


@pytest.mark.parametrize("max_check", [True])
def test_aliases_capped(max_check: bool) -> None:
    resolved = resolve_player_address(
        seed=_seed(user_address_name="primary"),
        profile=_profile("Alex", aliases=tuple(f"a{i}" for i in range(20))),
    )
    assert len(resolved.aliases) <= 6
