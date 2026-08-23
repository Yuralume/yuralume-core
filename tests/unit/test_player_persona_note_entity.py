"""PP2 — the declared-note entity's own rules.

Length is rejected rather than clipped on the way in: a half-stored world
premise is a setting the character would act on wrongly.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from kokoro_link.domain.entities.player_persona_note import (
    PLAYER_PERSONA_NOTE_MAX_CHARS,
    PlayerPersonaNote,
)


def test_note_is_trimmed() -> None:
    note = PlayerPersonaNote(
        character_id="c1", operator_id="alice", note="  我是超能力者  \n",
    )

    assert note.note == "我是超能力者"


def test_empty_note_is_rejected() -> None:
    with pytest.raises(ValueError):
        PlayerPersonaNote(character_id="c1", operator_id="alice", note="   ")


def test_note_at_the_ceiling_is_accepted() -> None:
    note = PlayerPersonaNote(
        character_id="c1",
        operator_id="alice",
        note="我" * PLAYER_PERSONA_NOTE_MAX_CHARS,
    )

    assert len(note.note) == PLAYER_PERSONA_NOTE_MAX_CHARS


def test_note_over_the_ceiling_is_rejected_not_clipped() -> None:
    with pytest.raises(ValueError):
        PlayerPersonaNote(
            character_id="c1",
            operator_id="alice",
            note="我" * (PLAYER_PERSONA_NOTE_MAX_CHARS + 1),
        )


def test_whitespace_padding_does_not_count_towards_the_ceiling() -> None:
    note = PlayerPersonaNote(
        character_id="c1",
        operator_id="alice",
        note="  " + "我" * PLAYER_PERSONA_NOTE_MAX_CHARS + "  ",
    )

    assert len(note.note) == PLAYER_PERSONA_NOTE_MAX_CHARS


def test_create_stamps_updated_at() -> None:
    stamped = datetime(2026, 8, 17, tzinfo=timezone.utc)

    note = PlayerPersonaNote.create(
        character_id="c1",
        operator_id="alice",
        note="我是超能力者",
        updated_at=stamped,
    )

    assert note.updated_at == stamped
    assert PlayerPersonaNote.create(
        character_id="c1", operator_id="alice", note="我是超能力者",
    ).updated_at is not None
