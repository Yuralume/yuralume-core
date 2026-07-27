"""Migration smoke for scheduler tick-journal idempotency (SoakFix-A)."""

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
    / "l7j4k9m1013_scheduler_tick_journal_dedup.py"
).resolve()


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("tick_journal_dedup", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _RecordingOp:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.indexes: dict[str, dict[str, Any]] = {}

    def execute(self, statement: Any) -> None:
        self.calls.append(("execute", str(statement)))

    def create_index(self, name: str, table: str, columns: Any, **kwargs: Any) -> None:
        self.calls.append(("create_index", name))
        self.indexes[name] = kwargs

    def drop_index(self, name: str, **kwargs: Any) -> None:
        self.calls.append(("drop_index", name))


def test_revision_chains_to_execution_mode() -> None:
    migration = _load_migration()
    assert migration.revision == "l7j4k9m1013"
    assert migration.down_revision == "k6i3g8h01012"


def test_upgrade_dedups_before_partial_unique_indexes(monkeypatch) -> None:  # noqa: ANN001
    migration = _load_migration()
    recorder = _RecordingOp()
    monkeypatch.setattr(migration, "op", recorder)

    migration.upgrade()

    assert recorder.calls[0][0] == "execute"
    assert [name for verb, name in recorder.calls[1:]] == [
        "uq_scheduler_tick_journal_character",
        "uq_scheduler_tick_journal_social",
    ]
    assert recorder.indexes["uq_scheduler_tick_journal_character"]["unique"] is True
    assert "postgresql_where" in recorder.indexes["uq_scheduler_tick_journal_character"]
    assert "postgresql_where" in recorder.indexes["uq_scheduler_tick_journal_social"]


def test_downgrade_drops_both_unique_indexes(monkeypatch) -> None:  # noqa: ANN001
    migration = _load_migration()
    recorder = _RecordingOp()
    monkeypatch.setattr(migration, "op", recorder)

    migration.downgrade()

    assert [name for verb, name in recorder.calls] == [
        "uq_scheduler_tick_journal_social",
        "uq_scheduler_tick_journal_character",
    ]
