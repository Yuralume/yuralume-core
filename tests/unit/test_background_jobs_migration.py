"""Migration smoke for the background-job queue foundation (i4g1e6f80810).

Recording-op pattern (see test_character_identity_migration): swap the
alembic ``op`` for a stub that records calls, run upgrade()/downgrade(),
and assert the additive tables + partial indexes are created and dropped.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

from kokoro_link.contracts.background_jobs import summarize_error


MIGRATION_PATH = (
    Path(__file__).parents[2]
    / "alembic"
    / "versions"
    / "i4g1e6f80810_background_job_queue.py"
)


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "test_background_jobs_migration_module", MIGRATION_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _RecordingOp:
    def __init__(self) -> None:
        self.created_tables: list[str] = []
        self.dropped_tables: list[str] = []
        self.created_indexes: list[tuple[str, str, dict[str, Any]]] = []
        self.dropped_indexes: list[str] = []

    def create_table(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.created_tables.append(name)

    def drop_table(self, name: str) -> None:
        self.dropped_tables.append(name)

    def create_index(
        self, name: str, table: str, columns: Any, **kwargs: Any,
    ) -> None:
        self.created_indexes.append((name, table, kwargs))

    def drop_index(self, name: str, **kwargs: Any) -> None:
        self.dropped_indexes.append(name)


_EXPECTED_TABLES = [
    "background_jobs",
    "background_runtime_leases",
    "background_coordinator_cursors",
    "scheduler_tick_journal",
    "realtime_events",
]


def test_revision_chains_to_prior_head() -> None:
    migration = _load_migration()
    assert migration.revision == "i4g1e6f80810"
    assert migration.down_revision == "h3f0d5e70809"


def test_upgrade_creates_all_tables_and_partial_indexes(monkeypatch) -> None:  # noqa: ANN001
    migration = _load_migration()
    recorder = _RecordingOp()
    monkeypatch.setattr(migration, "op", recorder)

    migration.upgrade()

    assert recorder.created_tables == _EXPECTED_TABLES

    idx = {name: kwargs for name, _table, kwargs in recorder.created_indexes}
    # Ready-queue and idempotency indexes are PostgreSQL partial indexes.
    assert "postgresql_where" in idx["ix_background_jobs_ready"]
    assert idx["uq_background_jobs_active_idem"]["unique"] is True
    assert "postgresql_where" in idx["uq_background_jobs_active_idem"]


def test_downgrade_drops_all_tables(monkeypatch) -> None:  # noqa: ANN001
    migration = _load_migration()
    recorder = _RecordingOp()
    monkeypatch.setattr(migration, "op", recorder)

    migration.downgrade()

    assert set(recorder.dropped_tables) == set(_EXPECTED_TABLES)


def test_summarize_error_sanitizes_and_never_empty() -> None:
    assert summarize_error(ValueError("boom")) == "ValueError: boom"
    assert summarize_error("") == "UnknownError"
    long_token = "x" * 40
    summary = summarize_error(f"failed key={long_token}")
    assert long_token not in summary
    assert "[redacted]" in summary
