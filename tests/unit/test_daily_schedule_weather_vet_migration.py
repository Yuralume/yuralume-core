"""Migration smoke for the ``daily_schedules`` weather-vet marker (y0n7h4l10026).

Recording-op pattern (see test_rss_source_region_locked_migration): swap the
alembic ``op`` for a stub that records calls, run upgrade()/downgrade(), and
assert the two additive columns. Both must be nullable with no server
default — ``NULL`` is the honest "this day was never vetted" value for every
row that exists at upgrade time, so no backfill and no table rewrite.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

from alembic.config import Config
from alembic.script import ScriptDirectory


REVISION = "y0n7h4l10026"
DOWN_REVISION = "x9m6g3k10025"

REPO_ROOT = Path(__file__).parents[2].resolve()
MIGRATION_PATH = (
    REPO_ROOT / "alembic" / "versions" / f"{REVISION}_daily_schedule_weather_vet.py"
)

VET_COLUMNS = ("weather_vet_activity_id", "weather_vet_condition")


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "test_daily_schedule_weather_vet_migration_module", MIGRATION_PATH,
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


def test_upgrade_adds_two_nullable_marker_columns() -> None:
    recorder = _run("upgrade")

    for name in VET_COLUMNS:
        assert ("add_column", "daily_schedules", name) in recorder.calls
        column = recorder.added[f"daily_schedules.{name}"]
        # Nullable, no default: an existing day has genuinely never been
        # vetted, and the gate must open on the first tick after upgrade.
        assert column.nullable is True
        assert column.server_default is None


def test_downgrade_drops_both_columns() -> None:
    recorder = _run("downgrade")

    for name in VET_COLUMNS:
        assert ("drop_column", "daily_schedules", name) in recorder.calls
