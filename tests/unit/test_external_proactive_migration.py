"""Migration smoke for the external proactive pre-send ledger (o0m7n2p3016).

Recording-op pattern (see test_background_jobs_migration): swap the alembic
``op`` for a stub that records calls, run upgrade()/downgrade(), and assert the
single additive table + its ``(state, expires_at)`` index are created / dropped.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any


MIGRATION_PATH = (
    Path(__file__).parents[2]
    / "alembic"
    / "versions"
    / "o0m7n2p3016_external_proactive_events.py"
)


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "test_external_proactive_migration_module", MIGRATION_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _RecordingOp:
    def __init__(self) -> None:
        self.created_tables: list[str] = []
        self.dropped_tables: list[str] = []
        self.created_indexes: list[tuple[str, str, list[str]]] = []
        self.dropped_indexes: list[str] = []

    def create_table(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.created_tables.append(name)

    def drop_table(self, name: str) -> None:
        self.dropped_tables.append(name)

    def create_index(
        self, name: str, table: str, columns: Any, **kwargs: Any,
    ) -> None:
        self.created_indexes.append((name, table, list(columns)))

    def drop_index(self, name: str, **kwargs: Any) -> None:
        self.dropped_indexes.append(name)


def test_revision_chains_to_prior_head() -> None:
    migration = _load_migration()
    assert migration.revision == "o0m7n2p3016"
    assert migration.down_revision == "n9l6m1o2015"


def test_upgrade_creates_table_and_index(monkeypatch) -> None:  # noqa: ANN001
    migration = _load_migration()
    recorder = _RecordingOp()
    monkeypatch.setattr(migration, "op", recorder)

    migration.upgrade()

    assert recorder.created_tables == ["external_proactive_events"]
    assert recorder.created_indexes == [
        (
            "ix_external_proactive_events_state_expires",
            "external_proactive_events",
            ["state", "expires_at"],
        ),
    ]


def test_downgrade_drops_table_and_index(monkeypatch) -> None:  # noqa: ANN001
    migration = _load_migration()
    recorder = _RecordingOp()
    monkeypatch.setattr(migration, "op", recorder)

    migration.downgrade()

    assert recorder.dropped_tables == ["external_proactive_events"]
    assert recorder.dropped_indexes == [
        "ix_external_proactive_events_state_expires",
    ]
