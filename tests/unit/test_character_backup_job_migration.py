"""Migration smoke for ``character_backup_jobs`` (f8w5q2t10034).

Recording-op pattern (see test_background_jobs_migration): swap the
alembic ``op`` for a stub that records calls, run upgrade()/downgrade(),
and assert the additive table + the partial in-flight-export unique
index are created and dropped. The partial index must declare BOTH
``postgresql_where`` and ``sqlite_where`` — the portable shape
``uq_story_arcs_active_character`` established.
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
    / "f8w5q2t10034_character_backup_jobs.py"
)

RESTORE_DEDUP_MIGRATION_PATH = (
    Path(__file__).parents[2]
    / "alembic"
    / "versions"
    / "g9x6r3u10035_character_backup_restore_operator_dedup.py"
)


def _load_module(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        f"test_character_backup_job_migration_{path.stem}", path,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_migration() -> ModuleType:
    return _load_module(MIGRATION_PATH)


def _load_restore_dedup_migration() -> ModuleType:
    return _load_module(RESTORE_DEDUP_MIGRATION_PATH)


class _RecordingOp:
    def __init__(self) -> None:
        self.created_tables: list[tuple[str, list[str]]] = []
        self.dropped_tables: list[str] = []
        self.created_indexes: list[tuple[str, str, Any, dict]] = []
        self.dropped_indexes: list[str] = []

    def create_table(self, name: str, *columns: Any, **kwargs: Any) -> None:
        column_names = [
            column.name for column in columns if hasattr(column, "name")
        ]
        self.created_tables.append((name, column_names))

    def drop_table(self, name: str) -> None:
        self.dropped_tables.append(name)

    def create_index(
        self, name: str, table: str, columns: Any, **kwargs: Any,
    ) -> None:
        self.created_indexes.append((name, table, columns, kwargs))

    def drop_index(self, name: str, **kwargs: Any) -> None:
        self.dropped_indexes.append(name)


def test_revision_chains_to_prior_head() -> None:
    migration = _load_migration()
    assert migration.revision == "f8w5q2t10034"
    assert migration.down_revision == "e7v4p8s10033"


def test_upgrade_creates_table_with_expected_columns(monkeypatch) -> None:  # noqa: ANN001
    migration = _load_migration()
    recorder = _RecordingOp()
    monkeypatch.setattr(migration, "op", recorder)

    migration.upgrade()

    assert [name for name, _cols in recorder.created_tables] == [
        "character_backup_jobs",
    ]
    (_name, columns), = recorder.created_tables
    assert columns == [
        "id",
        "kind",
        "character_id",
        "operator_id",
        "status",
        "attempts",
        "progress_json",
        "payload_json",
        "error",
        "artifact_object_key",
        "created_at",
        "updated_at",
        "finished_at",
    ]


def test_upgrade_creates_portable_partial_unique_index(monkeypatch) -> None:  # noqa: ANN001
    migration = _load_migration()
    recorder = _RecordingOp()
    monkeypatch.setattr(migration, "op", recorder)

    migration.upgrade()

    indexes = {
        name: (columns, kwargs)
        for name, _table, columns, kwargs in recorder.created_indexes
    }
    columns, kwargs = indexes["uq_character_backup_jobs_active_export"]
    assert columns == ["character_id"]
    assert kwargs["unique"] is True
    # Partial on BOTH dialects — the dedup invariant must not be a
    # Postgres-only nicety.
    assert "postgresql_where" in kwargs
    assert "sqlite_where" in kwargs
    for clause in (kwargs["postgresql_where"], kwargs["sqlite_where"]):
        assert "kind = 'export'" in str(clause)
        assert "status = 'running'" in str(clause)

    assert "ix_character_backup_jobs_status_updated" in indexes
    assert "ix_character_backup_jobs_operator_created" in indexes
    assert "ix_character_backup_jobs_character_id" in indexes


def test_downgrade_drops_everything(monkeypatch) -> None:  # noqa: ANN001
    migration = _load_migration()
    recorder = _RecordingOp()
    monkeypatch.setattr(migration, "op", recorder)

    migration.downgrade()

    assert recorder.dropped_tables == ["character_backup_jobs"]
    assert set(recorder.dropped_indexes) == {
        "uq_character_backup_jobs_active_export",
        "ix_character_backup_jobs_status_updated",
        "ix_character_backup_jobs_operator_created",
        "ix_character_backup_jobs_character_id",
    }


# ---------------------------------------------------------------------------
# g9x6r3u10035 — per-operator in-flight-restore dedup (A2)
# ---------------------------------------------------------------------------


def test_restore_dedup_revision_chains_to_prior_head() -> None:
    migration = _load_restore_dedup_migration()
    assert migration.revision == "g9x6r3u10035"
    assert migration.down_revision == "f8w5q2t10034"


def test_restore_dedup_creates_portable_partial_unique_index(
    monkeypatch,  # noqa: ANN001
) -> None:
    migration = _load_restore_dedup_migration()
    recorder = _RecordingOp()
    monkeypatch.setattr(migration, "op", recorder)

    migration.upgrade()

    ((name, table, columns, kwargs),) = recorder.created_indexes
    assert name == "uq_character_backup_jobs_active_restore"
    assert table == "character_backup_jobs"
    assert columns == ["operator_id"]
    assert kwargs["unique"] is True
    # Partial on BOTH dialects — the invariant must not be Postgres-only.
    assert "postgresql_where" in kwargs
    assert "sqlite_where" in kwargs
    for clause in (kwargs["postgresql_where"], kwargs["sqlite_where"]):
        assert "kind = 'restore'" in str(clause)
        assert "status = 'running'" in str(clause)


def test_restore_dedup_downgrade_drops_the_index(monkeypatch) -> None:  # noqa: ANN001
    migration = _load_restore_dedup_migration()
    recorder = _RecordingOp()
    monkeypatch.setattr(migration, "op", recorder)

    migration.downgrade()

    assert recorder.dropped_indexes == [
        "uq_character_backup_jobs_active_restore",
    ]
    assert recorder.dropped_tables == []


def test_orm_model_matches_migration_shape() -> None:
    """The hand-written migration and the ORM must agree (they are
    maintained separately — this is the drift alarm)."""
    from kokoro_link.infrastructure.persistence.character_backup_models import (
        CharacterBackupJobRow,
    )

    table = CharacterBackupJobRow.__table__
    assert table.name == "character_backup_jobs"
    assert [column.name for column in table.columns] == [
        "id",
        "kind",
        "character_id",
        "operator_id",
        "status",
        "attempts",
        "progress_json",
        "payload_json",
        "error",
        "artifact_object_key",
        "created_at",
        "updated_at",
        "finished_at",
    ]
    index_names = {index.name for index in table.indexes}
    assert "uq_character_backup_jobs_active_export" in index_names
    assert "uq_character_backup_jobs_active_restore" in index_names
    assert "ix_character_backup_jobs_status_updated" in index_names
    assert "ix_character_backup_jobs_operator_created" in index_names
