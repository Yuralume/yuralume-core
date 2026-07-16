"""The prompt builder consumes resolved addresses (Step 6 wiring)."""

from __future__ import annotations

from types import SimpleNamespace

from kokoro_link.domain.entities.character_operator_relationship_seed import (
    CharacterOperatorRelationshipSeed,
)
from kokoro_link.domain.entities.operator_profile import OperatorProfile
from kokoro_link.domain.value_objects.resolved_address import (
    AddressProvenance,
    ResolvedAddress,
)
from kokoro_link.infrastructure.prompt.default import (
    _render_operator_block,
    _render_register_block,
)
from kokoro_link.infrastructure.prompt.initial_relationship import (
    render_initial_relationship_seed_lines,
)


def test_operator_block_uses_resolved_primary_over_display_name() -> None:
    operator = OperatorProfile(id="op1", display_name="alex_4821")
    resolved = ResolvedAddress(
        primary="阿力",
        aliases=("alex_4821",),
        provenance=AddressProvenance.EXPLICIT_SEED,
    )
    lines = _render_operator_block(operator, resolved)
    text = "\n".join(lines)
    assert "稱呼：阿力" in text          # resolved per-char name wins
    assert "稱呼：alex_4821" not in text  # raw platform label demoted
    assert "別稱：alex_4821" in text     # old/platform label kept as alias


def test_operator_block_without_resolver_keeps_legacy_behaviour() -> None:
    operator = OperatorProfile(id="op1", display_name="Alex")
    lines = _render_operator_block(operator)  # no resolved → legacy path
    assert "- 稱呼：Alex" in "\n".join(lines)


def test_operator_block_fallback_renders_nothing() -> None:
    # Placeholder operator + a fallback resolution (no seed/persona/real
    # name anywhere) → the block stays quiet so the legacy 「使用者」
    # wording elsewhere still reads naturally.
    operator = OperatorProfile.default()
    fallback = ResolvedAddress(primary="對方", provenance=AddressProvenance.FALLBACK)
    assert _render_operator_block(operator, fallback) == []


def test_initial_relationship_can_drop_address_lines() -> None:
    seed = CharacterOperatorRelationshipSeed(
        character_id="c1",
        operator_id="op1",
        relationship_label="朋友",
        user_address_name="阿力",
        character_address_name="美緒",
    )
    with_address = render_initial_relationship_seed_lines(seed)
    without_address = render_initial_relationship_seed_lines(
        seed, include_address=False,
    )
    assert any("稱呼使用者：阿力" in line for line in with_address)
    assert any("使用者怎麼稱呼你：美緒" in line for line in with_address)
    assert not any("稱呼使用者" in line for line in without_address)
    assert not any("使用者怎麼稱呼你" in line for line in without_address)
    # the relationship context itself is still rendered
    assert any("關係：朋友" in line for line in without_address)


def test_register_block_surfaces_seed_salutation_without_observation() -> None:
    resolved = ResolvedAddress(
        primary="美緒姐", provenance=AddressProvenance.EXPLICIT_SEED,
    )
    lines = _render_register_block(
        character=SimpleNamespace(name="美緒", operator_pace_preference=None),
        address_preference=None,  # nothing observed yet
        resolved_character_address=resolved,
    )
    assert any("對方稱呼你：美緒姐" in line for line in lines)


def test_register_block_ignores_character_name_fallback() -> None:
    # A pure character-name fallback must NOT inject an assumed salutation.
    resolved = ResolvedAddress(
        primary="美緒", provenance=AddressProvenance.CHARACTER_NAME,
    )
    lines = _render_register_block(
        character=SimpleNamespace(name="美緒", operator_pace_preference=None),
        address_preference=None,
        resolved_character_address=resolved,
    )
    assert not any("對方稱呼你" in line for line in lines)
