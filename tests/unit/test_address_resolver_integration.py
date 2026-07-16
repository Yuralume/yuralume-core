"""Step 10 — cross-cutting invariants for the bidirectional resolver +
rename log that span more than one module.

The per-step unit tests already cover precedence, alias churn idempotency,
and cloud-locked display-name protection. These lock the *integration*
guarantees the plan calls out as risks (§7):

- the resolver is read-only — resolving never mutates the global profile
  (per-character isolation: a per-char name must not leak to siblings);
- the rename log render is per-(character, operator, direction) isolated;
- an explicit seed name structurally outranks a stale learned persona
  name, so a seed edit can't double-render the primary even before the
  persona reconcile runs.
"""

from __future__ import annotations

import asyncio
import copy
from datetime import datetime, timezone

from kokoro_link.domain.entities.character_operator_relationship_seed import (
    CharacterOperatorRelationshipSeed,
)
from kokoro_link.domain.entities.operator_persona import OperatorPersona
from kokoro_link.domain.entities.operator_profile import OperatorProfile
from kokoro_link.domain.services.address_resolver import resolve_player_address
from kokoro_link.domain.value_objects.address_change_event import (
    DIRECTION_CHARACTER,
    DIRECTION_PLAYER,
    AddressChangeEvent,
)
from kokoro_link.domain.value_objects.profile_field import (
    EvidenceRef,
    ProfileField,
)
from kokoro_link.domain.value_objects.resolved_address import AddressProvenance
from kokoro_link.infrastructure.prompt.address_change import (
    render_address_change_lines,
)
from kokoro_link.infrastructure.repositories.in_memory_address_change_log import (
    InMemoryAddressChangeLogRepository,
)


def _learned_name(value: str) -> ProfileField:
    return ProfileField(
        character_id="c1",
        field_key="name",
        layer=1,
        value=value,
        confidence=0.9,
        evidence_refs=(
            EvidenceRef(
                turn_id="t1",
                conversation_id="conv1",
                quote=value,
                extracted_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
            ),
        ),
        last_updated=datetime(2026, 6, 1, tzinfo=timezone.utc),
        update_count=3,
        source="extraction",
    )


def test_resolver_does_not_mutate_global_profile() -> None:
    # Per-character isolation: a per-char seed name overrides the prompt
    # term but must NEVER be written back to the shared global profile.
    profile = OperatorProfile(id="op1", display_name="alex_4821")
    before = copy.deepcopy(profile)
    seed = CharacterOperatorRelationshipSeed(
        character_id="c1", operator_id="op1", user_address_name="阿力",
    )

    resolved = resolve_player_address(seed=seed, profile=profile)

    assert resolved.primary == "阿力"
    assert resolved.provenance is AddressProvenance.EXPLICIT_SEED
    # Global profile is untouched — the per-char name stays local.
    assert profile.display_name == before.display_name == "alex_4821"
    assert profile.aliases == before.aliases


def test_seed_name_outranks_stale_persona_name() -> None:
    # Even if a learned persona still holds the OLD name, an explicit seed
    # edit wins the primary slot — the structural guard against
    # double-rendering the primary before persona reconcile runs.
    persona = OperatorPersona(
        character_id="c1",
        operator_id="op1",
        layer1_identity={"name": _learned_name("艾力")},
    )
    seed = CharacterOperatorRelationshipSeed(
        character_id="c1", operator_id="op1", user_address_name="阿力",
    )
    resolved = resolve_player_address(
        seed=seed,
        persona=persona,
        profile=OperatorProfile(id="op1", display_name="alex_4821"),
    )
    assert resolved.primary == "阿力"  # new seed name wins
    assert "艾力" in resolved.aliases  # old learned name kept as alias


def test_rename_log_render_is_per_pair_isolated() -> None:
    repo = InMemoryAddressChangeLogRepository()

    async def scenario() -> None:
        await repo.record(
            AddressChangeEvent(
                character_id="cA",
                operator_id="op1",
                direction=DIRECTION_PLAYER,
                old_value="艾力",
                new_value="阿力",
                effective_at=datetime(2026, 6, 29, tzinfo=timezone.utc),
            ),
        )
        # Character A sees its own rename.
        a_event = await repo.latest(
            character_id="cA", operator_id="op1", direction=DIRECTION_PLAYER,
        )
        assert a_event is not None
        assert render_address_change_lines(player_event=a_event)

        # Character B (same operator) has no rename of its own — must stay
        # quiet, never inheriting A's event.
        b_event = await repo.latest(
            character_id="cB", operator_id="op1", direction=DIRECTION_PLAYER,
        )
        assert b_event is None
        assert render_address_change_lines(player_event=b_event) == []

    asyncio.run(scenario())


def test_rename_log_latest_is_per_direction() -> None:
    repo = InMemoryAddressChangeLogRepository()

    async def scenario() -> None:
        await repo.record(
            AddressChangeEvent(
                character_id="cA", operator_id="op1",
                direction=DIRECTION_PLAYER, old_value="艾力", new_value="阿力",
                effective_at=datetime(2026, 6, 28, tzinfo=timezone.utc),
            ),
        )
        await repo.record(
            AddressChangeEvent(
                character_id="cA", operator_id="op1",
                direction=DIRECTION_CHARACTER, old_value="美緒", new_value="美緒姐",
                effective_at=datetime(2026, 6, 29, tzinfo=timezone.utc),
            ),
        )
        player = await repo.latest(
            character_id="cA", operator_id="op1", direction=DIRECTION_PLAYER,
        )
        character = await repo.latest(
            character_id="cA", operator_id="op1", direction=DIRECTION_CHARACTER,
        )
        assert player.new_value == "阿力"
        assert character.new_value == "美緒姐"

    asyncio.run(scenario())
