"""Migration contract for tenant-authoritative Cloud subscription locks."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any


MIGRATION_PATH = (
    Path(__file__).parents[2]
    / "alembic"
    / "versions"
    / "d2b5e8f90404_cloud_subscription_lock.py"
)


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "test_cloud_subscription_lock_migration_module",
        MIGRATION_PATH,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _RecordingOp:
    def __init__(self) -> None:
        self.created_tables: list[tuple[str, tuple[Any, ...]]] = []
        self.added_columns: list[tuple[str, Any]] = []
        self.statements: list[str] = []
        self.dropped_columns: list[tuple[str, str]] = []
        self.dropped_tables: list[str] = []

    def create_table(self, name: str, *parts: Any) -> None:
        self.created_tables.append((name, parts))

    def add_column(self, table_name: str, column: Any) -> None:
        self.added_columns.append((table_name, column))

    def execute(self, statement: str) -> None:
        self.statements.append(statement)

    def drop_column(self, table_name: str, column_name: str) -> None:
        self.dropped_columns.append((table_name, column_name))

    def drop_table(self, table_name: str) -> None:
        self.dropped_tables.append(table_name)


def test_chain_targets_subscription_lapse_migration() -> None:
    migration = _load_migration()

    assert migration.revision == "d2b5e8f90404"
    assert migration.down_revision == "c1a4d7e80303"


def test_upgrade_adds_authoritative_state_and_projection(monkeypatch) -> None:  # noqa: ANN001
    migration = _load_migration()
    recorder = _RecordingOp()
    monkeypatch.setattr(migration, "op", recorder)

    migration.upgrade()

    assert [name for name, _parts in recorder.created_tables] == [
        "cloud_subscription_states",
    ]
    assert [(table, column.name) for table, column in recorder.added_columns] == [
        ("characters", "subscription_locked"),
    ]
    projection = recorder.added_columns[0][1]
    assert projection.nullable is False
    assert projection.server_default is not None

    assert len(recorder.statements) == 2
    tenant_upsert, legacy_projection = recorder.statements
    assert "ON CONFLICT (tenant_id) DO UPDATE" in tenant_upsert
    assert "frozen_reason = 'subscription_lapse'" in tenant_upsert
    assert "SET subscription_locked = TRUE" in legacy_projection
    assert "AND EXISTS" in legacy_projection
    assert "BTRIM(opf.cloud_tenant_id) <> ''" in legacy_projection


def test_downgrade_restores_legacy_lock_before_dropping_schema(monkeypatch) -> None:  # noqa: ANN001
    migration = _load_migration()
    recorder = _RecordingOp()
    monkeypatch.setattr(migration, "op", recorder)

    migration.downgrade()

    assert "frozen_reason = 'subscription_lapse'" in recorder.statements[0]
    assert recorder.dropped_columns == [("characters", "subscription_locked")]
    assert recorder.dropped_tables == ["cloud_subscription_states"]
