"""Migration smoke for character identity fields."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any


MIGRATION_PATH = (
    Path(__file__).parents[2]
    / "alembic"
    / "versions"
    / "e4k6l1m20081_character_identity_fields.py"
)


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "test_character_identity_migration_module",
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


def test_upgrade_adds_identity_columns(monkeypatch) -> None:  # noqa: ANN001
    migration = _load_migration()
    recorder = _RecordingOp()
    monkeypatch.setattr(migration, "op", recorder)

    migration.upgrade()

    names = [column.name for table, column in recorder.added_columns]
    assert [table for table, _column in recorder.added_columns] == [
        "characters", "characters", "characters",
    ]
    assert names == [
        "gender_identity",
        "third_person_pronoun",
        "visual_gender_presentation",
    ]
    for _table, column in recorder.added_columns:
        assert column.nullable is False
        assert column.server_default is not None
        assert str(column.server_default.arg) == ""


def test_downgrade_drops_identity_columns(monkeypatch) -> None:  # noqa: ANN001
    migration = _load_migration()
    recorder = _RecordingOp()
    monkeypatch.setattr(migration, "op", recorder)

    migration.downgrade()

    assert recorder.dropped_columns == [
        ("characters", "visual_gender_presentation"),
        ("characters", "third_person_pronoun"),
        ("characters", "gender_identity"),
    ]
