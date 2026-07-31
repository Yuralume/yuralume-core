"""Migration smoke for ``story_seeds`` tier/regions (z1p8i5m10027).

Recording-op pattern (see test_daily_schedule_weather_vet_migration):
swap alembic ``op`` for a stub, run upgrade()/downgrade(), assert the
two additive columns. Both are NOT NULL with a server default so every
pre-existing seed lands on the behaviour-preserving values — ``daily``
(the historical, only tier) and ``["global"]`` (drawable everywhere,
exactly how every seed behaved before regions existed).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

from alembic.config import Config
from alembic.script import ScriptDirectory


REVISION = "z1p8i5m10027"
DOWN_REVISION = "y0n7h4l10026"

REPO_ROOT = Path(__file__).parents[2].resolve()
MIGRATION_PATH = (
    REPO_ROOT / "alembic" / "versions" / f"{REVISION}_story_seed_tier_regions.py"
)


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "test_story_seed_tier_regions_migration_module", MIGRATION_PATH,
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


def test_upgrade_adds_tier_with_daily_server_default() -> None:
    recorder = _run("upgrade")

    assert ("add_column", "story_seeds", "tier") in recorder.calls
    column = recorder.added["story_seeds.tier"]
    assert column.nullable is False
    assert column.server_default is not None
    assert column.server_default.arg == "daily"


def test_upgrade_adds_regions_with_global_server_default() -> None:
    recorder = _run("upgrade")

    assert ("add_column", "story_seeds", "regions") in recorder.calls
    column = recorder.added["story_seeds.regions"]
    assert column.nullable is False
    assert column.server_default is not None
    # Same JSON-in-Text convention as world_frames' '["any"]'.
    assert column.server_default.arg == '["global"]'


def test_downgrade_drops_both_columns() -> None:
    recorder = _run("downgrade")

    assert ("drop_column", "story_seeds", "regions") in recorder.calls
    assert ("drop_column", "story_seeds", "tier") in recorder.calls
