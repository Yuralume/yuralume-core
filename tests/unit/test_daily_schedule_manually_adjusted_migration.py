"""Migration smoke for ``daily_schedules.manually_adjusted`` (u1i7c4f10046).

Recording-op pattern (see test_daily_schedule_weather_vet_migration): swap
the alembic ``op`` for a stub that records calls, run upgrade()/downgrade(),
and assert the single additive column. Non-null with server_default false —
every row that exists at upgrade time has never been manually edited, so
``false`` is the honest backfill and no table rewrite is needed.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

from alembic.config import Config
from alembic.script import ScriptDirectory


REVISION = "u1i7c4f10046"
DOWN_REVISION = "t0h6b3e10045"

REPO_ROOT = Path(__file__).parents[2].resolve()
MIGRATION_PATH = (
    REPO_ROOT / "alembic" / "versions"
    / f"{REVISION}_daily_schedule_manually_adjusted.py"
)

COLUMN = "manually_adjusted"


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "test_daily_schedule_manually_adjusted_migration_module", MIGRATION_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _RecordingOp:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.added: dict[str, Any] = {}

    def add_column(self, table: str, column: Any) -> None:
        self.calls.append(("add_column", table, column.name))
        self.added[f"{table}.{column.name}"] = column

    def drop_column(self, table: str, name: str) -> None:
        self.calls.append(("drop_column", table, name))


def _run(direction: str) -> _RecordingOp:
    migration = _load_migration()
    recorder = _RecordingOp()
    original_op = migration.op
    migration.op = recorder
    try:
        getattr(migration, direction)()
    finally:
        migration.op = original_op
    return recorder


def test_revision_chains_to_prior_head() -> None:
    migration = _load_migration()
    assert migration.revision == REVISION
    assert migration.down_revision == DOWN_REVISION


def test_revision_graph_has_a_single_head_containing_this_revision() -> None:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    script = ScriptDirectory.from_config(config)

    heads = list(script.get_heads())
    assert len(heads) == 1
    ancestry = {
        revision.revision
        for revision in script.iterate_revisions(heads[0], "base")
    }
    assert REVISION in ancestry


def test_upgrade_adds_non_null_flag_with_false_backfill() -> None:
    recorder = _run("upgrade")

    assert ("add_column", "daily_schedules", COLUMN) in recorder.calls
    column = recorder.added[f"daily_schedules.{COLUMN}"]
    assert column.nullable is False
    assert column.server_default is not None
    assert "false" in str(column.server_default.arg).lower()


def test_downgrade_drops_the_column() -> None:
    recorder = _run("downgrade")
    assert ("drop_column", "daily_schedules", COLUMN) in recorder.calls
