"""Migration smoke for dropping ``operator_profiles.current_status`` (PP1)."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any


MIGRATION_PATH = (
    Path(__file__).parents[1]
    / ".."
    / "alembic"
    / "versions"
    / "p6d2x9a10041_drop_operator_current_status.py"
).resolve()


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "drop_operator_current_status", MIGRATION_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _RecordingBatchOp:
    def __init__(self) -> None:
        self.dropped: list[str] = []
        self.added: list[str] = []

    def drop_column(self, name: str) -> None:
        self.dropped.append(name)

    def add_column(self, column: Any) -> None:
        self.added.append(column.name)


class _RecordingBatchContext:
    def __init__(self, batch_op: _RecordingBatchOp) -> None:
        self._batch_op = batch_op

    def __enter__(self) -> _RecordingBatchOp:
        return self._batch_op

    def __exit__(self, *exc_info: Any) -> None:
        return None


class _RecordingOp:
    def __init__(self) -> None:
        self.batch_op = _RecordingBatchOp()
        self.table_names: list[str] = []

    def batch_alter_table(self, table_name: str) -> _RecordingBatchContext:
        self.table_names.append(table_name)
        return _RecordingBatchContext(self.batch_op)


def test_revision_chains_to_the_player_persona_notes_head() -> None:
    migration = _load_migration()

    assert migration.revision == "p6d2x9a10041"
    assert migration.down_revision == "n5c1w8z10040"


def test_upgrade_drops_both_columns_via_batch_mode(monkeypatch) -> None:  # noqa: ANN001
    migration = _load_migration()
    recorder = _RecordingOp()
    monkeypatch.setattr(migration, "op", recorder)

    migration.upgrade()

    assert recorder.table_names == ["operator_profiles"]
    assert set(recorder.batch_op.dropped) == {
        "current_status",
        "current_status_set_at",
    }


def test_downgrade_adds_both_columns_back_nullable(monkeypatch) -> None:  # noqa: ANN001
    migration = _load_migration()
    recorder = _RecordingOp()
    monkeypatch.setattr(migration, "op", recorder)

    migration.downgrade()

    assert recorder.table_names == ["operator_profiles"]
    assert set(recorder.batch_op.added) == {
        "current_status",
        "current_status_set_at",
    }
