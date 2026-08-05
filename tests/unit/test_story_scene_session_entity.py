"""Invariants of the scene session entity (SC1-A).

Small surface, but three of these rules are what SC1-C/D/E will lean on:
activity time only ever moves forward, a close is idempotent and keeps the
*first* reason, and the vocabularies are closed so a typo cannot invent a
status nothing knows how to end.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.domain.entities.story_arc import (
    OPERATOR_POSITION_ABSENT,
    OPERATOR_POSITION_CENTRAL,
    OPERATOR_POSITION_PRESENT,
)
from kokoro_link.domain.entities.story_scene_session import (
    SCENE_CLOSE_MANUAL,
    SCENE_CLOSE_TIMEOUT,
    SCENE_CLOSED,
    SCENE_LAYER_BEAT,
    SCENE_OPEN,
    StorySceneSession,
)


NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


def _scene(**overrides) -> StorySceneSession:  # noqa: ANN003
    base = dict(
        character_id="c1",
        conversation_id="conv-1",
        source_layer=SCENE_LAYER_BEAT,
        opened_at=NOW,
    )
    base.update(overrides)
    return StorySceneSession.open_scene(**base)


def test_a_fresh_scene_is_open_and_idle_from_its_own_start() -> None:
    scene = _scene()

    assert scene.status == SCENE_OPEN
    assert scene.is_open
    assert scene.last_activity_at == NOW
    assert scene.closed_at is None and scene.closed_reason is None


def test_activity_time_only_moves_forward() -> None:
    """A slower replica's late write must not make a live scene look
    stale to the SC1-E timeout closer."""
    scene = _scene()
    later = scene.touched(NOW + timedelta(minutes=5))

    assert later.last_activity_at == NOW + timedelta(minutes=5)
    assert later.touched(NOW - timedelta(hours=3)).last_activity_at == (
        NOW + timedelta(minutes=5)
    )


def test_naive_timestamps_are_normalised_to_utc() -> None:
    """SQLite hands back naive datetimes for a tz-aware column; every
    later comparison has to keep working."""
    scene = _scene(opened_at=datetime(2026, 6, 1, 12, 0))

    assert scene.opened_at == NOW
    assert scene.touched(datetime(2026, 6, 1, 13, 0)).last_activity_at == (
        NOW + timedelta(hours=1)
    )


def test_closing_is_terminal_and_keeps_the_first_reason() -> None:
    """A manual end that races the timeout closer must not be rewritten
    into a timeout (or the other way round)."""
    closed = _scene().closed(reason=SCENE_CLOSE_MANUAL, at=NOW)
    again = closed.closed(reason=SCENE_CLOSE_TIMEOUT, at=NOW + timedelta(days=1))

    assert again.status == SCENE_CLOSED
    assert again.closed_reason == SCENE_CLOSE_MANUAL
    assert again.closed_at == NOW
    assert not again.is_open


@pytest.mark.parametrize(
    "kwargs",
    [
        {"source_layer": "made_up"},
        {"status": "paused"},
        {"character_id": ""},
        {"conversation_id": ""},
    ],
    ids=["layer", "status", "no-character", "no-conversation"],
)
def test_invalid_construction_is_rejected(kwargs: dict) -> None:
    base = dict(
        id="s1",
        character_id="c1",
        conversation_id="conv-1",
        source_layer=SCENE_LAYER_BEAT,
        opened_at=NOW,
    )
    base.update(kwargs)

    with pytest.raises(ValueError):
        StorySceneSession(**base)


def test_an_open_scene_cannot_carry_a_close_reason() -> None:
    with pytest.raises(ValueError):
        StorySceneSession(
            id="s1",
            character_id="c1",
            conversation_id="conv-1",
            source_layer=SCENE_LAYER_BEAT,
            status=SCENE_OPEN,
            closed_reason=SCENE_CLOSE_MANUAL,
            opened_at=NOW,
        )


def test_blank_frame_labels_normalise_to_none() -> None:
    scene = _scene(title="  ", location="   ", mood="", dramatic_question=" ")

    assert scene.title == ""
    assert scene.location is None
    assert scene.mood is None
    assert scene.dramatic_question is None


# ── player position (OP2-D) ──────────────────────────────────────────


def test_a_fresh_scene_defaults_to_an_unjudged_player_position() -> None:
    scene = _scene()

    assert scene.operator_position is None
    assert scene.operator_note is None


@pytest.mark.parametrize(
    "position",
    [OPERATOR_POSITION_ABSENT, OPERATOR_POSITION_PRESENT, OPERATOR_POSITION_CENTRAL],
)
def test_the_three_known_positions_are_accepted(position: str) -> None:
    scene = _scene(operator_position=position, operator_note="她要向你坦白")

    assert scene.operator_position == position
    assert scene.operator_note == "她要向你坦白"


def test_position_casing_and_whitespace_are_normalised() -> None:
    scene = _scene(operator_position=f"  {OPERATOR_POSITION_CENTRAL.upper()}  ")

    assert scene.operator_position == OPERATOR_POSITION_CENTRAL


def test_blank_position_and_note_normalise_to_none() -> None:
    scene = _scene(operator_position="  ", operator_note="   ")

    assert scene.operator_position is None
    assert scene.operator_note is None


def test_an_off_vocabulary_position_is_rejected() -> None:
    """Structural — a value nothing branches on would silently take a
    branch nobody chose, same rule as ``StoryArcBeat.operator_position``."""
    with pytest.raises(ValueError):
        _scene(operator_position="bystander")
