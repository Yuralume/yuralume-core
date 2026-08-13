"""Migration smoke for ``characters.origin_official_card_id`` (EC2-A)."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any


MIGRATION_PATH = (
    Path(__file__).parents[2]
    / "alembic"
    / "versions"
    / "j2a9u6x10038_character_origin_official_card.py"
)


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "test_character_origin_official_card_migration_module",
        MIGRATION_PATH,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _RecordingOp:
    def __init__(self) -> None:
        self.added_columns: list[tuple[str, Any]] = []
        self.dropped_columns: list[tuple[str, str]] = []

    def add_column(self, table_name: str, column: Any) -> None:
        self.added_columns.append((table_name, column))

    def drop_column(self, table_name: str, column_name: str) -> None:
        self.dropped_columns.append((table_name, column_name))


def test_revision_chains_onto_the_previous_head() -> None:
    migration = _load_migration()

    assert migration.revision == "j2a9u6x10038"
    assert migration.down_revision == "i1z8t5w10037"


def test_upgrade_adds_a_nullable_provenance_column(monkeypatch) -> None:  # noqa: ANN001
    migration = _load_migration()
    recorder = _RecordingOp()
    monkeypatch.setattr(migration, "op", recorder)

    migration.upgrade()

    assert [table for table, _column in recorder.added_columns] == ["characters"]
    column = recorder.added_columns[0][1]
    assert column.name == "origin_official_card_id"
    assert column.type.length == 128
    # Nullable with no server default: every existing row — and every
    # self-host row, forever — stays NULL, which is "not managed".
    assert column.nullable is True
    assert column.server_default is None
    assert not column.foreign_keys


def test_downgrade_drops_the_column(monkeypatch) -> None:  # noqa: ANN001
    migration = _load_migration()
    recorder = _RecordingOp()
    monkeypatch.setattr(migration, "op", recorder)

    migration.downgrade()

    assert recorder.dropped_columns == [
        ("characters", "origin_official_card_id"),
    ]
