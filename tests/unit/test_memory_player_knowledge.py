"""KB5 — ``MemoryItem.player_knowledge`` field and persistence round-trip.

Covers: entity normalization/defaults, the SA row mapping round-trip
(including legacy ``NULL`` rows normalizing to ``""``), and the
character-backup DTO carrying the column through export/import.

This ticket only wires the field and its persistence — it does not
change what any write station records (KB6) or how any surface renders
the value (KB7); those are separate tickets on
``PLAYER_KNOWLEDGE_BOUNDARY_PLAN``.
"""

from __future__ import annotations

from datetime import datetime, timezone

from kokoro_link.application.dto.character_backup.memory import (
    MemoryItemBackupRecord,
)
from kokoro_link.domain.entities.memory_item import MemoryItem
from kokoro_link.domain.value_objects.memory_kind import MemoryKind
from kokoro_link.infrastructure.persistence.models import MemoryItemRow
from kokoro_link.infrastructure.persistence.sa_memory_mapping import (
    item_to_row,
    row_to_item,
)

_NOW = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)


# --- entity --------------------------------------------------------------


def test_player_knowledge_normalizes_and_defaults() -> None:
    private = MemoryItem.create(
        character_id="c1", kind=MemoryKind.SEMANTIC,
        content="角色獨自完成的行程", player_knowledge="PRIVATE",
    )
    assert private.player_knowledge == "private"

    disclosed = MemoryItem.create(
        character_id="c1", kind=MemoryKind.SEMANTIC,
        content="角色後來跟玩家說了", player_knowledge="Disclosed",
    )
    assert disclosed.player_knowledge == "disclosed"

    shared = MemoryItem.create(
        character_id="c1", kind=MemoryKind.EPISODIC,
        content="兩人一起經歷的事", player_knowledge="shared",
    )
    assert shared.player_knowledge == "shared"

    unjudged = MemoryItem.create(
        character_id="c1", kind=MemoryKind.SEMANTIC, content="x",
    )
    assert unjudged.player_knowledge == ""  # default = legacy/unjudged

    garbage = MemoryItem.create(
        character_id="c1", kind=MemoryKind.SEMANTIC, content="x",
        player_knowledge="public",
    )
    assert garbage.player_knowledge == ""  # unknown value coerced to ""


# --- SA mapping round-trip ------------------------------------------------


def test_player_knowledge_round_trips_through_sa_mapping() -> None:
    item = MemoryItem.create(
        character_id="c1", kind=MemoryKind.SEMANTIC,
        content="角色獨自完成的行程", player_knowledge="private",
    )
    restored = row_to_item(item_to_row(item))
    assert restored.player_knowledge == "private"


def test_player_knowledge_unjudged_writes_as_null_column() -> None:
    """The domain default ``""`` is persisted as ``NULL`` — legacy rows
    and freshly-written unjudged rows share one representation at the
    DB layer, matching the KB2 ``source_beat_id`` precedent."""
    item = MemoryItem.create(
        character_id="c1", kind=MemoryKind.SEMANTIC, content="x",
    )
    row = item_to_row(item)
    assert row.player_knowledge is None


def test_legacy_null_row_normalizes_to_empty_string() -> None:
    """A pre-migration row (constructed here with the column left unset,
    the honest state of every row that predates the migration) reads
    back as ``""`` — the legacy / unjudged sentinel — not ``None``."""
    row = MemoryItemRow(
        id="mem-legacy",
        character_id="c1",
        conversation_id=None,
        kind="semantic",
        content="舊資料",
        salience=0.5,
        tags="[]",
        created_at=_NOW,
        last_accessed_at=None,
        access_count=0,
        embedding=None,
        tags_embedding=None,
        participants_json="[]",
        world_id=None,
        location=None,
        audience="",
    )
    assert row.player_knowledge is None  # honest pre-migration state
    item = row_to_item(row)
    assert item.player_knowledge == ""


# --- character-backup DTO --------------------------------------------------


def test_backup_record_carries_player_knowledge() -> None:
    row = MemoryItemRow(
        id="mem-1",
        character_id="char-1",
        conversation_id=None,
        kind="episodic",
        content="兩人一起去看了流星雨",
        salience=0.9,
        tags="[]",
        created_at=_NOW,
        last_accessed_at=None,
        access_count=0,
        embedding=None,
        tags_embedding=None,
        participants_json="[]",
        world_id=None,
        location=None,
        audience="",
        player_knowledge="shared",
    )
    record = MemoryItemBackupRecord.from_row(row)
    assert record.player_knowledge == "shared"

    kwargs = record.to_row_kwargs()
    rebuilt = MemoryItemRow(**kwargs)
    assert rebuilt.player_knowledge == "shared"


def test_backup_record_carries_null_player_knowledge() -> None:
    """A legacy/unclassified row exports and restores with ``None``
    intact — the DTO does not normalize on export (that stays the
    application-layer mapping's job), it just carries the raw column."""
    row = MemoryItemRow(
        id="mem-2",
        character_id="char-1",
        conversation_id=None,
        kind="episodic",
        content="舊資料",
        salience=0.5,
        tags="[]",
        created_at=_NOW,
        last_accessed_at=None,
        access_count=0,
        embedding=None,
        tags_embedding=None,
        participants_json="[]",
        world_id=None,
        location=None,
        audience="",
    )
    record = MemoryItemBackupRecord.from_row(row)
    assert record.player_knowledge is None
    kwargs = record.to_row_kwargs()
    rebuilt = MemoryItemRow(**kwargs)
    assert rebuilt.player_knowledge is None
