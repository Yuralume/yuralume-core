"""StoryArc / StoryArcBeat persistence mapping (AE2 + SC0).

Mirrors ``test_story_seed_persistence_mapping.py``'s shape for the
sibling ``tier``/``regions`` JSON-in-Text fields: exercise the SA
mapping functions directly (``_arc_to_row`` / ``_row_to_arc`` /
``_apply_arc_updates``) against fake row objects rather than a live
engine, so the legacy-row / malformed-JSON backward-compat paths don't
have to fight the ORM's NOT NULL column constraint to be exercised.

The SC0 section at the bottom does the same for the beat-level retry
budget columns (``play_failure_count`` / ``last_play_failure_at``),
including the turn-snapshot round trip — an undo that dropped the
counters would silently hand a beat a fresh retry budget.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone

from kokoro_link.application.services.turn_snapshot_codec import (
    arc_from_dict,
    arc_to_dict,
)
from kokoro_link.domain.entities.story_arc import (
    OPERATOR_POSITION_CENTRAL,
    PLAY_RESULT_FAILED,
    StoryArc,
    StoryArcBeat,
)
from kokoro_link.infrastructure.persistence.sa_story_arc_repository import (
    _apply_arc_updates,
    _arc_to_row,
    _beat_to_row,
    _row_to_arc,
    _row_to_beat,
)


def _arc(**overrides) -> StoryArc:
    defaults = dict(
        character_id="char-1",
        title="主線",
        premise="premise",
        theme="custom",
        start_date=date(2026, 5, 1),
        end_date=date(2026, 5, 21),
        source_seed_ids=["seed-a", "seed-b"],
    )
    defaults.update(overrides)
    return StoryArc.create(**defaults)


def test_arc_to_row_serializes_source_seed_ids() -> None:
    row = _arc_to_row(_arc())
    assert json.loads(row.source_seed_ids) == ["seed-a", "seed-b"]


def test_row_to_arc_round_trips_source_seed_ids() -> None:
    original = _arc()
    restored = _row_to_arc(_arc_to_row(original), [])
    assert restored.source_seed_ids == ("seed-a", "seed-b")


def test_row_to_arc_defaults_to_empty_tuple_when_no_seed_ids() -> None:
    restored = _row_to_arc(_arc_to_row(_arc(source_seed_ids=[])), [])
    assert restored.source_seed_ids == ()


def test_apply_arc_updates_writes_source_seed_ids() -> None:
    row = _arc_to_row(_arc(source_seed_ids=["seed-a"]))
    _apply_arc_updates(row, _arc(source_seed_ids=["seed-c"]))
    assert json.loads(row.source_seed_ids) == ["seed-c"]


def test_row_to_arc_defaults_when_column_missing() -> None:
    """Pre-AE2-migration rows loaded by old ORM state fall back to the
    historical behaviour: no known seed provenance."""

    now = datetime.now(timezone.utc)

    class _LegacyRow:
        id = "legacy-1"
        character_id = "char-1"
        title = "舊主線"
        premise = "premise"
        theme = "custom"
        tone = "daily"
        source_template_id = None
        start_date = "2026-05-01"
        end_date = "2026-05-21"
        status = "active"
        created_at = now
        updated_at = now

    restored = _row_to_arc(_LegacyRow(), [])
    assert restored.source_seed_ids == ()


def test_row_to_arc_tolerates_malformed_source_seed_ids_json() -> None:
    row = _arc_to_row(_arc())
    row.source_seed_ids = "not-json"
    restored = _row_to_arc(row, [])
    assert restored.source_seed_ids == ()


def test_row_to_arc_tolerates_null_source_seed_ids() -> None:
    """A NULL slipping past the NOT NULL default (raw SQL, driver
    quirk) must degrade to () rather than crash arc load — same
    forgiving contract as malformed JSON."""
    row = _arc_to_row(_arc())
    row.source_seed_ids = None
    restored = _row_to_arc(row, [])
    assert restored.source_seed_ids == ()

# --- SC0 retry-budget columns ----------------------------------------


def _beat(**overrides) -> StoryArcBeat:
    defaults = dict(
        arc_id="arc-1",
        sequence=0,
        scheduled_date=date(2026, 5, 1),
        title="公告張貼",
        summary="她發現公告欄釘了一張新海報。",
    )
    defaults.update(overrides)
    return StoryArcBeat.create(**defaults)


def test_beat_row_round_trips_the_failure_counters() -> None:
    failed_at = datetime(2026, 5, 1, 8, 0, tzinfo=timezone.utc)
    beat = _beat().with_play_attempt(
        attempted_at=failed_at,
        source="scene_simulation",
        result=PLAY_RESULT_FAILED,
        push_intensity="autonomous_scene",
    )

    restored = _row_to_beat(_beat_to_row("arc-1", beat))

    assert restored.play_failure_count == 1
    assert restored.last_play_failure_at == failed_at


def test_row_to_beat_defaults_when_failure_columns_missing() -> None:
    """Pre-SC0 ORM state reads back as an unspent budget.

    Same stance as the migration's non-failure rows: absence of the
    column means "no known failures", never an invented one."""

    class _LegacyRow:
        id = "legacy-beat"
        arc_id = "arc-1"
        sequence = 0
        scheduled_date = "2026-05-01"
        title = "舊 beat"
        summary = "summary"
        tension = "setup"
        status = "pending"
        realized_event_id = None
        play_attempt_count = 7
        last_play_attempt_at = None
        last_play_attempt_source = None
        last_play_attempt_result = None
        last_play_push_intensity = None
        scene_characters = "[]"
        location = None
        dramatic_question = None
        scene_type = "encounter"
        required = True

    restored = _row_to_beat(_LegacyRow())

    assert restored.play_attempt_count == 7
    assert restored.play_failure_count == 0
    assert restored.last_play_failure_at is None


# --- OP0-A player-position columns -----------------------------------


def test_beat_row_round_trips_the_player_position_pair() -> None:
    beat = _beat(
        operator_position=OPERATOR_POSITION_CENTRAL,
        operator_note="她要向你坦白",
    )

    row = _beat_to_row("arc-1", beat)
    assert row.operator_position == OPERATOR_POSITION_CENTRAL
    assert row.operator_note == "她要向你坦白"

    restored = _row_to_beat(row)
    assert restored.operator_position == OPERATOR_POSITION_CENTRAL
    assert restored.operator_note == "她要向你坦白"


def test_beat_row_keeps_unjudged_as_null_not_empty_string() -> None:
    """``NULL`` is the domain's fourth state, so it has to survive the
    round trip as ``None`` — an empty string would look like a decision
    to every consumer that branches on the column."""
    row = _beat_to_row("arc-1", _beat())

    assert row.operator_position is None
    assert row.operator_note is None
    restored = _row_to_beat(row)
    assert restored.operator_position is None
    assert restored.operator_note is None


def test_row_to_beat_defaults_when_position_columns_missing() -> None:
    """Pre-OP0 ORM state reads back as unjudged — never as a guess."""

    class _LegacyRow:
        id = "legacy-beat"
        arc_id = "arc-1"
        sequence = 0
        scheduled_date = "2026-05-01"
        title = "舊 beat"
        summary = "summary"
        tension = "setup"
        status = "pending"
        realized_event_id = None
        play_attempt_count = 0
        last_play_attempt_at = None
        last_play_attempt_source = None
        last_play_attempt_result = None
        last_play_push_intensity = None
        scene_characters = "[]"
        location = None
        dramatic_question = None
        scene_type = "encounter"
        required = True

    restored = _row_to_beat(_LegacyRow())

    assert restored.operator_position is None
    assert restored.operator_note is None


def test_row_to_beat_degrades_an_unreadable_position_to_unjudged() -> None:
    """A hand-edited row (or one written by a newer Core) must not take
    a whole arc down — the entity would raise, so the decode neutralises
    it into the state that means "nobody decided"."""
    row = _beat_to_row("arc-1", _beat())
    row.operator_position = "protagonist"

    restored = _row_to_beat(row)

    assert restored.operator_position is None


def test_turn_snapshot_round_trips_the_failure_counters() -> None:
    """Undo must not hand a beat a free retry budget."""
    failed_at = datetime(2026, 5, 1, 8, 0, tzinfo=timezone.utc)
    arc = _arc().with_beats([
        _beat().with_play_attempt(
            attempted_at=failed_at,
            source="scene_simulation",
            result=PLAY_RESULT_FAILED,
            push_intensity="autonomous_scene",
        ),
    ])

    restored = arc_from_dict(arc_to_dict(arc))

    assert restored.beats[0].play_failure_count == 1
    assert restored.beats[0].last_play_failure_at == failed_at


def test_turn_snapshot_round_trips_the_player_position_pair() -> None:
    """OP0-B: the turn-undo snapshot must not drop the player-position
    pair — an undo that dropped it would silently regress a judged beat
    back to "unjudged" the moment the player rewinds."""
    arc = _arc().with_beats([
        _beat(
            operator_position=OPERATOR_POSITION_CENTRAL,
            operator_note="她要向你坦白",
        ),
    ])

    restored = arc_from_dict(arc_to_dict(arc))

    assert restored.beats[0].operator_position == OPERATOR_POSITION_CENTRAL
    assert restored.beats[0].operator_note == "她要向你坦白"


def test_turn_snapshot_keeps_unjudged_beats_unjudged() -> None:
    arc = _arc().with_beats([_beat()])

    restored = arc_from_dict(arc_to_dict(arc))

    assert restored.beats[0].operator_position is None
    assert restored.beats[0].operator_note is None
