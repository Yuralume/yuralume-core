"""Unit tests for the persona value objects.

Covers the validation invariants that other layers rely on:

- ``ProfileField`` requires evidence and a layer it can be rendered as.
- ``EvidenceRef`` round-trips losslessly through ``to_dict`` /
  ``from_dict``.
- ``CandidateField`` enforces the same layer set and rejects bad
  state strings.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from kokoro_link.domain.value_objects.familiarity import Familiarity
from kokoro_link.domain.value_objects.profile_field import (
    CandidateField,
    EvidenceRef,
    ProfileField,
)


_CHAR_ID = "char-test"


def _ev(quote: str = "我是工程師") -> EvidenceRef:
    return EvidenceRef(
        turn_id="conv-1:0",
        conversation_id="conv-1",
        quote=quote,
        extracted_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def test_profile_field_rejects_layer_4():
    """Layer 4 is computed, not stored — constructing one as a field
    should fail so a stray write can't slip through."""
    with pytest.raises(ValueError):
        ProfileField(
            field_key="dummy",
            layer=4,
            value="x",
            confidence=0.5,
            evidence_refs=(_ev(),),
            last_updated=datetime.now(timezone.utc),
            update_count=1,
            source="extraction",
            character_id=_CHAR_ID,
        )


def test_profile_field_requires_evidence():
    with pytest.raises(ValueError):
        ProfileField(
            field_key="occupation",
            layer=1,
            value="engineer",
            confidence=0.7,
            evidence_refs=(),
            last_updated=datetime.now(timezone.utc),
            update_count=1,
            source="extraction",
            character_id=_CHAR_ID,
        )


def test_profile_field_confidence_out_of_range_rejected():
    with pytest.raises(ValueError):
        ProfileField(
            field_key="occupation",
            layer=1,
            value="engineer",
            confidence=1.5,
            evidence_refs=(_ev(),),
            last_updated=datetime.now(timezone.utc),
            update_count=1,
            source="extraction",
            character_id=_CHAR_ID,
        )


def test_evidence_ref_round_trips():
    original = _ev()
    decoded = EvidenceRef.from_dict(original.to_dict())
    assert decoded == original


def test_evidence_ref_caps_long_quote():
    long_quote = "我" * 1000
    ref = EvidenceRef(
        turn_id="t",
        conversation_id="c",
        quote=long_quote,
        extracted_at=datetime.now(timezone.utc),
    )
    assert len(ref.quote) <= 240


def test_candidate_field_default_state_is_pending():
    cand = CandidateField(
        field_key="age",
        layer=1,
        proposed_value="30",
        evidence_ref=_ev(),
        raw_extractor_confidence=0.7,
        character_id=_CHAR_ID,
    )
    assert cand.state == "pending"


def test_candidate_field_rejects_unknown_state():
    with pytest.raises(ValueError):
        CandidateField(
            field_key="age",
            layer=1,
            proposed_value="30",
            evidence_ref=_ev(),
            raw_extractor_confidence=0.7,
            state="unknown",
            character_id=_CHAR_ID,
        )


def test_profile_field_rejects_missing_character_id():
    """character_id is part of the identity tuple — missing it is a
    bug (would write a row that no character can claim)."""
    with pytest.raises(ValueError):
        ProfileField(
            field_key="occupation",
            layer=1,
            value="engineer",
            confidence=0.7,
            evidence_refs=(_ev(),),
            last_updated=datetime.now(timezone.utc),
            update_count=1,
            source="extraction",
        )


def test_familiarity_known_values():
    assert Familiarity("stranger") == Familiarity.STRANGER
    assert Familiarity("CLOSE") == Familiarity.CLOSE
    with pytest.raises(ValueError):
        Familiarity("bff")
