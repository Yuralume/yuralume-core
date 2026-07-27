"""Migration smoke for the RSS region dimension (v7t4u9w0023).

Recording-op pattern (see test_inbound_message_receipts_migration): swap
the alembic ``op`` for a stub that records calls in order, run
upgrade()/downgrade(), and assert the two additive nullable columns plus
the lookup index the curator's region narrowing rides on. Also pins that
the revision graph still has exactly one head — parallel branches would
break ``alembic upgrade head`` on deploy.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

from alembic.config import Config
from alembic.script import ScriptDirectory


REVISION = "v7t4u9w0023"
DOWN_REVISION = "u6s3t8v9022"

REPO_ROOT = Path(__file__).parents[2].resolve()
MIGRATION_PATH = REPO_ROOT / "alembic" / "versions" / f"{REVISION}_rss_region.py"


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "test_rss_region_migration_module", MIGRATION_PATH,
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

    def create_index(
        self, name: str, table: str, columns: Any, **kwargs: Any,
    ) -> None:
        self.calls.append(("create_index", table, name))

    def drop_index(self, name: str, **kwargs: Any) -> None:
        self.calls.append(("drop_index", kwargs.get("table_name", ""), name))


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
    # Later migrations legitimately move the head forward; what must hold
    # is that this revision stays on the single chain that head walks
    # back through, i.e. no parallel branch was opened after it.
    ancestry = {
        revision.revision
        for revision in script.iterate_revisions(heads[0], "base")
    }
    assert REVISION in ancestry


def test_upgrade_adds_nullable_region_columns() -> None:
    migration = _load_migration()
    recorder = _RecordingOp()
    original_op = migration.op
    migration.op = recorder
    try:
        migration.upgrade()
    finally:
        migration.op = original_op

    assert ("add_column", "rss_sources", "region") in recorder.calls
    assert ("add_column", "world_events", "source_region") in recorder.calls
    assert (
        "create_index", "world_events", "ix_world_events_source_region",
    ) in recorder.calls
    # NULL is the "global source" zero value; a NOT NULL column would
    # force a backfill decision onto every existing self-host row.
    for key in ("rss_sources.region", "world_events.source_region"):
        assert recorder.added[key].nullable is True


def test_downgrade_drops_both_columns_and_index() -> None:
    migration = _load_migration()
    recorder = _RecordingOp()
    original_op = migration.op
    migration.op = recorder
    try:
        migration.downgrade()
    finally:
        migration.op = original_op

    assert recorder.calls[0][0] == "drop_index"
    assert ("drop_column", "world_events", "source_region") in recorder.calls
    assert ("drop_column", "rss_sources", "region") in recorder.calls
