"""IC1 — :class:`PlayerIdentityCard` validation.

The card is a copy of the creation intake, so its ceilings and its policy
enum must be the seed's, not a second set that happens to match today.
These tests assert the *shared constants*, never literal numbers.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from kokoro_link.domain.entities.character_operator_relationship_seed import (
    MAX_LABEL_CHARS,
    MAX_LIVING_ARRANGEMENT_CHARS,
    MAX_TEXT_CHARS,
    SCHEDULE_INVOLVEMENT_POLICIES,
    SEED_TEXT_FIELD_MAX_CHARS,
    CharacterOperatorRelationshipSeed,
)
from kokoro_link.domain.entities.player_identity_card import (
    PLAYER_IDENTITY_CARD_CONTENT_FIELDS,
    PLAYER_IDENTITY_CARD_NAME_MAX_CHARS,
    PlayerIdentityCard,
)
from kokoro_link.domain.entities.player_persona_note import (
    PLAYER_PERSONA_NOTE_MAX_CHARS,
)


def _card(**overrides: object) -> PlayerIdentityCard:
    return PlayerIdentityCard.create(
        operator_id="alice", name="上班族的我", **overrides,
    )


def test_create_stamps_an_id_and_both_timestamps() -> None:
    now = datetime(2026, 8, 27, 9, 0, tzinfo=timezone.utc)

    card = PlayerIdentityCard.create(
        operator_id="alice", name="上班族的我", now=now,
    )

    assert card.id
    assert card.created_at == now
    assert card.updated_at == now


def test_name_is_trimmed_and_required() -> None:
    assert _card().name == "上班族的我"
    assert PlayerIdentityCard.create(
        operator_id="alice", name="  異世界勇者的我  ",
    ).name == "異世界勇者的我"

    with pytest.raises(ValueError):
        PlayerIdentityCard.create(operator_id="alice", name="   ")


def test_name_over_the_ceiling_is_rejected() -> None:
    at_ceiling = "名" * PLAYER_IDENTITY_CARD_NAME_MAX_CHARS

    assert PlayerIdentityCard.create(
        operator_id="alice", name=at_ceiling,
    ).name == at_ceiling

    with pytest.raises(ValueError):
        PlayerIdentityCard.create(operator_id="alice", name=at_ceiling + "名")


def test_operator_id_is_required() -> None:
    with pytest.raises(ValueError):
        PlayerIdentityCard.create(operator_id="  ", name="上班族的我")


def test_seed_text_fields_are_trimmed_and_clipped_at_the_seed_ceilings() -> None:
    card = _card(
        relationship_label="  同事  ",
        known_context="脈" * (MAX_TEXT_CHARS + 50),
        living_arrangement="住" * (MAX_LIVING_ARRANGEMENT_CHARS + 10),
    )

    assert card.relationship_label == "同事"
    assert len(card.known_context) == MAX_TEXT_CHARS
    assert len(card.living_arrangement) == MAX_LIVING_ARRANGEMENT_CHARS
    # Clipping matches the seed's own normalization, field for field.
    seed = CharacterOperatorRelationshipSeed(
        character_id="c1",
        operator_id="alice",
        relationship_label="  同事  ",
        known_context="脈" * (MAX_TEXT_CHARS + 50),
        living_arrangement="住" * (MAX_LIVING_ARRANGEMENT_CHARS + 10),
    )
    assert card.relationship_label == seed.relationship_label
    assert card.known_context == seed.known_context
    assert card.living_arrangement == seed.living_arrangement


def test_every_seed_text_field_uses_the_shared_ceiling() -> None:
    """No field may quietly get its own limit."""
    for field, max_chars in SEED_TEXT_FIELD_MAX_CHARS.items():
        card = _card(**{field: "字" * (max_chars + 5)})
        assert len(getattr(card, field)) == max_chars, field


def test_label_ceiling_is_the_seed_ceiling_not_a_copy() -> None:
    card = _card(relationship_label="標" * (MAX_LABEL_CHARS + 3))

    assert len(card.relationship_label) == MAX_LABEL_CHARS


def test_schedule_policy_is_normalized_and_validated() -> None:
    assert _card(schedule_involvement_policy="  SHARED_ALLOWED ").\
        schedule_involvement_policy == "shared_allowed"
    assert _card().schedule_involvement_policy == "none"
    assert _card(schedule_involvement_policy="").schedule_involvement_policy == "none"

    with pytest.raises(ValueError):
        _card(schedule_involvement_policy="whatever")


def test_every_policy_the_seed_accepts_is_accepted_here() -> None:
    for policy in SCHEDULE_INVOLVEMENT_POLICIES:
        assert _card(
            schedule_involvement_policy=policy,
        ).schedule_involvement_policy == policy


def test_persona_note_is_trimmed_and_rejected_over_the_pp_ceiling() -> None:
    assert _card(persona_note="  我是超能力者  ").persona_note == "我是超能力者"
    at_ceiling = "設" * PLAYER_PERSONA_NOTE_MAX_CHARS
    assert _card(persona_note=at_ceiling).persona_note == at_ceiling

    with pytest.raises(ValueError):
        _card(persona_note=at_ceiling + "設")


def test_create_rejects_unknown_content_fields() -> None:
    with pytest.raises(ValueError):
        _card(confirmed_by_user=True)


def test_content_field_list_matches_the_dataclass() -> None:
    """The list the DTO and the overwrite path iterate must stay complete."""
    card = _card()
    identity = {"id", "operator_id", "name", "created_at", "updated_at"}
    declared = {field for field in card.__slots__ if field not in identity}

    assert declared == set(PLAYER_IDENTITY_CARD_CONTENT_FIELDS)


def test_renamed_keeps_identity_and_content_and_bumps_updated_at() -> None:
    created = datetime(2026, 8, 27, 9, 0, tzinfo=timezone.utc)
    later = datetime(2026, 8, 28, 9, 0, tzinfo=timezone.utc)
    card = PlayerIdentityCard.create(
        operator_id="alice",
        name="上班族的我",
        now=created,
        known_context="同一間事務所",
    )

    renamed = card.renamed("  社畜的我  ", now=later)

    assert renamed.id == card.id
    assert renamed.name == "社畜的我"
    assert renamed.known_context == "同一間事務所"
    assert renamed.created_at == created
    assert renamed.updated_at == later


def test_overwritten_by_keeps_id_and_created_at() -> None:
    created = datetime(2026, 8, 27, 9, 0, tzinfo=timezone.utc)
    later = datetime(2026, 8, 28, 9, 0, tzinfo=timezone.utc)
    original = PlayerIdentityCard.create(
        operator_id="alice",
        name="上班族的我",
        now=created,
        known_context="舊的",
        persona_note="舊人設",
    )
    replacement = PlayerIdentityCard.create(
        operator_id="alice",
        name="上班族的我",
        known_context="新的",
        persona_note="新人設",
        proactive_permission=True,
    )

    merged = original.overwritten_by(replacement, now=later)

    assert merged.id == original.id
    assert merged.created_at == created
    assert merged.updated_at == later
    assert merged.known_context == "新的"
    assert merged.persona_note == "新人設"
    assert merged.proactive_permission is True
