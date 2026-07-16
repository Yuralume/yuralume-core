"""Migration smoke for character freeze columns (CHARACTER_FREEZE_PLAN)."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any


MIGRATION_PATH = (
    Path(__file__).parents[2]
    / "alembic"
    / "versions"
    / "b8d2e4f60302_character_freeze.py"
)


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "test_character_freeze_migration_module",
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


def test_chain_targets_previous_head() -> None:
    migration = _load_migration()
    assert migration.revision == "b8d2e4f60302"
    assert migration.down_revision == "f2b3d4e50206"


def test_upgrade_adds_freeze_columns(monkeypatch) -> None:  # noqa: ANN001
    migration = _load_migration()
    recorder = _RecordingOp()
    monkeypatch.setattr(migration, "op", recorder)

    migration.upgrade()

    assert [t for t, _c in recorder.added_columns] == [
        "characters", "characters", "characters",
    ]
    by_name = {c.name: c for _t, c in recorder.added_columns}

    frozen = by_name["frozen"]
    assert frozen.nullable is False
    assert frozen.server_default is not None

    frozen_at = by_name["frozen_at"]
    assert frozen_at.nullable is True

    created_at = by_name["created_at"]
    assert created_at.nullable is False
    # Server-managed default so existing rows backfill at migration time.
    assert created_at.server_default is not None


def test_downgrade_drops_freeze_columns(monkeypatch) -> None:  # noqa: ANN001
    migration = _load_migration()
    recorder = _RecordingOp()
    monkeypatch.setattr(migration, "op", recorder)

    migration.downgrade()

    assert recorder.dropped_columns == [
        ("characters", "created_at"),
        ("characters", "frozen_at"),
        ("characters", "frozen"),
    ]
