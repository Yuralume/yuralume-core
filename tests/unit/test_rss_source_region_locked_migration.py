"""Migration smoke for ``rss_sources.region_locked`` (x9m6g3k10025).

Recording-op pattern (see test_rss_region_migration): swap the alembic
``op`` for a stub that records calls, run upgrade()/downgrade(), and assert
the single additive column. The column must be NOT NULL with a ``false``
server default — every existing row is by definition operator-untouched,
so the upgrade must not need a backfill and must not lock anything.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

from alembic.config import Config
from alembic.script import ScriptDirectory


REVISION = "x9m6g3k10025"
DOWN_REVISION = "w8k5f2j10024"

REPO_ROOT = Path(__file__).parents[2].resolve()
MIGRATION_PATH = (
    REPO_ROOT / "alembic" / "versions" / f"{REVISION}_rss_source_region_locked.py"
)


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "test_rss_source_region_locked_migration_module", MIGRATION_PATH,
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


def test_upgrade_adds_a_non_null_false_defaulted_flag() -> None:
    migration = _load_migration()
    recorder = _RecordingOp()
    original_op = migration.op
    migration.op = recorder
    try:
        migration.upgrade()
    finally:
        migration.op = original_op

    assert ("add_column", "rss_sources", "region_locked") in recorder.calls
    column = recorder.added["rss_sources.region_locked"]
    assert column.nullable is False
    # Existing rows must land unlocked, or the seed could never backfill a
    # region onto a self-host that upgraded before the column existed.
    assert str(column.server_default.arg).lower() == "false"


def test_downgrade_drops_the_flag() -> None:
    migration = _load_migration()
    recorder = _RecordingOp()
    original_op = migration.op
    migration.op = recorder
    try:
        migration.downgrade()
    finally:
        migration.op = original_op

    assert ("drop_column", "rss_sources", "region_locked") in recorder.calls
