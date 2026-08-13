"""Migration smoke for ``deferred_intents.revisit_at`` (T2)."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any


MIGRATION_PATH = (
    Path(__file__).parents[2]
    / "alembic"
    / "versions"
    / "k3b0v7y10039_deferred_intent_revisit_at.py"
)


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "test_deferred_intent_revisit_at_migration_module",
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

    assert migration.revision == "k3b0v7y10039"
    assert migration.down_revision == "j2a9u6x10038"


def test_upgrade_adds_a_nullable_timestamp_column(monkeypatch) -> None:  # noqa: ANN001
    migration = _load_migration()
    recorder = _RecordingOp()
    monkeypatch.setattr(migration, "op", recorder)

    migration.upgrade()

    assert [table for table, _column in recorder.added_columns] == [
        "deferred_intents",
    ]
    column = recorder.added_columns[0][1]
    assert column.name == "revisit_at"
    assert column.type.timezone is True
    # Nullable with no server default: every motive without an
    # appointment — including every row written before T2 — stays NULL.
    assert column.nullable is True
    assert column.server_default is None


def test_downgrade_drops_the_column(monkeypatch) -> None:  # noqa: ANN001
    migration = _load_migration()
    recorder = _RecordingOp()
    monkeypatch.setattr(migration, "op", recorder)

    migration.downgrade()

    assert recorder.dropped_columns == [("deferred_intents", "revisit_at")]
