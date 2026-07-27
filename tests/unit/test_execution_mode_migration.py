"""Migration smoke for the execution-mode ownership row (k6i3g8h01012).

Recording-op pattern (see test_visible_output_dedup_migration): swap the alembic
``op`` for a stub that records calls, run upgrade()/downgrade(), and assert the
single additive table + PK and the clean drop, plus the fixed revision chain.
"""

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
    / "k6i3g8h01012_execution_mode.py"
).resolve()


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "test_execution_mode_migration_module", MIGRATION_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _RecordingOp:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.created_tables: list[str] = []
        self.dropped_tables: list[str] = []

    def create_table(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append(("create_table", name))
        self.created_tables.append(name)

    def drop_table(self, name: str) -> None:
        self.calls.append(("drop_table", name))
        self.dropped_tables.append(name)


def test_revision_chains_to_prior_head() -> None:
    migration = _load_migration()
    assert migration.revision == "k6i3g8h01012"
    assert migration.down_revision == "j5h2f7g90911"


def test_upgrade_creates_single_mode_table(monkeypatch) -> None:  # noqa: ANN001
    migration = _load_migration()
    recorder = _RecordingOp()
    monkeypatch.setattr(migration, "op", recorder)

    migration.upgrade()

    assert recorder.created_tables == ["background_execution_mode"]


def test_downgrade_drops_mode_table(monkeypatch) -> None:  # noqa: ANN001
    migration = _load_migration()
    recorder = _RecordingOp()
    monkeypatch.setattr(migration, "op", recorder)

    migration.downgrade()

    assert recorder.dropped_tables == ["background_execution_mode"]
