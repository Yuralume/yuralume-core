"""Unit tests for the OperatorPersona aggregate + InteractionStrength.

These check the structural invariants the rest of the system relies on
when it walks layers generically — get the wrong layer assigned to a
dict and the prompt renderer will silently drop the field.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from kokoro_link.domain.entities.operator_persona import (
    InteractionStrength,
    OperatorPersona,
)
from kokoro_link.domain.value_objects.familiarity import Familiarity
from kokoro_link.domain.value_objects.profile_field import (
    EvidenceRef,
    ProfileField,
)


_CHAR_ID = "char-test"
_OP_ID = "default"


def _field(field_key: str, layer: int, value: str = "v") -> ProfileField:
    return ProfileField(
        field_key=field_key,
        layer=layer,
        value=value,
        confidence=0.8,
        evidence_refs=(
            EvidenceRef(
                turn_id="t",
                conversation_id="c",
                quote="quote",
                extracted_at=datetime.now(timezone.utc),
            ),
        ),
        last_updated=datetime.now(timezone.utc),
        update_count=2,
        source="extraction",
        character_id=_CHAR_ID,
    )


def test_empty_persona_is_empty():
    persona = OperatorPersona.empty(_CHAR_ID, _OP_ID)
    assert persona.is_empty()


def test_persona_rejects_misfiled_layer():
    """Putting a layer-2 field into the layer-1 dict should raise so
    the prompt renderer never sees inconsistent data."""
    bad = _field("interests", 2)
    with pytest.raises(ValueError):
        OperatorPersona(
            character_id=_CHAR_ID,
            operator_id=_OP_ID,
            layer1_identity={"interests": bad},
        )


def test_persona_fields_by_layer_routes_correctly():
    persona = OperatorPersona(
        character_id=_CHAR_ID,
        operator_id=_OP_ID,
        layer1_identity={"name": _field("name", 1, "丹尼")},
        layer2_life={"interests": _field("interests", 2, "電影")},
    )
    assert persona.fields_by_layer(1)["name"].value == "丹尼"
    assert persona.fields_by_layer(2)["interests"].value == "電影"
    assert persona.fields_by_layer(3) == {}
    with pytest.raises(ValueError):
        persona.fields_by_layer(99)


def test_interaction_strength_empty_returns_stranger_band():
    strength = InteractionStrength.empty(
        _CHAR_ID, _OP_ID, now=datetime.now(timezone.utc),
    )
    assert strength.total_user_messages == 0
    assert strength.familiarity_band == Familiarity.STRANGER


def test_interaction_strength_rejects_negative_counts():
    with pytest.raises(ValueError):
        InteractionStrength(
            character_id=_CHAR_ID,
            operator_id=_OP_ID,
            first_message_at=datetime.now(timezone.utc),
            total_user_messages=-1,
            days_since_first_contact=0,
            messages_last_7_days=0,
            messages_last_30_days=0,
            longest_session_minutes=0,
            shared_arc_realized_count=0,
            shared_drama_count=0,
            familiarity_band=Familiarity.STRANGER,
            computed_at=datetime.now(timezone.utc),
        )


def test_persona_rejects_missing_character_id():
    """Per-character is the design — building an aggregate without
    a character_id would be a bug."""
    with pytest.raises(ValueError):
        OperatorPersona(character_id="", operator_id=_OP_ID)
