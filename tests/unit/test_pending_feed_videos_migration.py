"""Migration smoke for ``pending_feed_videos`` (h0y7s4v10036).

Recording-op pattern (see test_character_backup_job_migration): swap the
alembic ``op`` for a stub that records calls, run upgrade()/downgrade(),
and assert the additive table plus the sweep index.

The column set is asserted against the ORM row rather than a hand-written
list, because these two drifting apart is the failure mode that only
shows up on a real deployment: the SA repository writes columns the
migration never created.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

from kokoro_link.infrastructure.persistence.models import PendingFeedVideoRow


MIGRATION_PATH = (
    Path(__file__).parents[2]
    / "alembic"
    / "versions"
    / "h0y7s4v10036_pending_feed_videos.py"
)


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "test_pending_feed_videos_migration_module", MIGRATION_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _RecordingOp:
    def __init__(self) -> None:
        self.created_tables: list[tuple[str, list[str]]] = []
        self.dropped_tables: list[str] = []
        self.created_indexes: list[tuple[str, str, Any, dict]] = []
        self.dropped_indexes: list[str] = []

    def create_table(self, name: str, *columns: Any, **kwargs: Any) -> None:
        del kwargs
        self.created_tables.append((
            name,
            [column.name for column in columns if hasattr(column, "name")],
        ))

    def drop_table(self, name: str) -> None:
        self.dropped_tables.append(name)

    def create_index(
        self, name: str, table: str, columns: Any, **kwargs: Any,
    ) -> None:
        self.created_indexes.append((name, table, columns, kwargs))

    def drop_index(self, name: str, **kwargs: Any) -> None:
        del kwargs
        self.dropped_indexes.append(name)


def test_revision_chains_to_prior_head() -> None:
    migration = _load_migration()
    assert migration.revision == "h0y7s4v10036"
    assert migration.down_revision == "g9x6r3u10035"


def test_upgrade_creates_the_table_the_orm_expects(monkeypatch) -> None:  # noqa: ANN001
    migration = _load_migration()
    recorder = _RecordingOp()
    monkeypatch.setattr(migration, "op", recorder)

    migration.upgrade()

    assert [name for name, _cols in recorder.created_tables] == [
        "pending_feed_videos",
    ]
    (_name, columns), = recorder.created_tables
    assert set(columns) == {
        column.name for column in PendingFeedVideoRow.__table__.columns
    }


def test_upgrade_creates_the_sweep_index(monkeypatch) -> None:  # noqa: ANN001
    """``list_due`` is the only query the poll carriers run, and it runs on
    every tick / reconcile: (status, next_poll_at) must be indexed."""
    migration = _load_migration()
    recorder = _RecordingOp()
    monkeypatch.setattr(migration, "op", recorder)

    migration.upgrade()

    indexes = {
        name: columns for name, _table, columns, _kwargs
        in recorder.created_indexes
    }
    assert indexes["ix_pending_feed_videos_status_next_poll"] == [
        "status", "next_poll_at",
    ]
    assert "ix_pending_feed_videos_character_id" in indexes
    assert "ix_pending_feed_videos_job_id" in indexes


def test_downgrade_drops_everything(monkeypatch) -> None:  # noqa: ANN001
    migration = _load_migration()
    recorder = _RecordingOp()
    monkeypatch.setattr(migration, "op", recorder)

    migration.downgrade()

    assert recorder.dropped_tables == ["pending_feed_videos"]
    assert set(recorder.dropped_indexes) == {
        "ix_pending_feed_videos_status_next_poll",
        "ix_pending_feed_videos_status",
        "ix_pending_feed_videos_job_id",
        "ix_pending_feed_videos_character_id",
    }
