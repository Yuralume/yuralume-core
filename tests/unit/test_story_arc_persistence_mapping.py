"""StoryArc source_seed_ids persistence mapping (AE2).

Mirrors ``test_story_seed_persistence_mapping.py``'s shape for the
sibling ``tier``/``regions`` JSON-in-Text fields: exercise the SA
mapping functions directly (``_arc_to_row`` / ``_row_to_arc`` /
``_apply_arc_updates``) against fake row objects rather than a live
engine, so the legacy-row / malformed-JSON backward-compat paths don't
have to fight the ORM's NOT NULL column constraint to be exercised.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone

from kokoro_link.domain.entities.story_arc import StoryArc
from kokoro_link.infrastructure.persistence.sa_story_arc_repository import (
    _apply_arc_updates,
    _arc_to_row,
    _row_to_arc,
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
