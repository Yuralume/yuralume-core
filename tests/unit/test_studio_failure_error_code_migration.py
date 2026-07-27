"""Migration smoke for the studio failure error_code column (w8k5f2j10024).

Recording-op pattern (see test_rss_region_migration): swap the alembic
``op`` for a stub that records calls in order, run upgrade()/downgrade(),
and assert the two additive nullable columns the 202 + poll pipelines need
to report a player-actionable refusal. Also pins that the revision graph
still has exactly one head — parallel branches break ``alembic upgrade
head`` on deploy.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

from alembic.config import Config
from alembic.script import ScriptDirectory

from kokoro_link.application.services.studio_failure import (
    MAX_ERROR_CODE_CHARS,
)


REVISION = "w8k5f2j10024"
DOWN_REVISION = "v7t4u9w0023"

REPO_ROOT = Path(__file__).parents[2].resolve()
MIGRATION_PATH = (
    REPO_ROOT / "alembic" / "versions"
    / f"{REVISION}_studio_failure_error_code.py"
)

TABLES = ("fusion_stories", "branching_dramas")


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "test_studio_failure_error_code_migration_module", MIGRATION_PATH,
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
    # Later migrations legitimately move the head forward; what must hold
    # is that this revision stays on the single chain that head walks
    # back through, i.e. no parallel branch was opened after it.
    ancestry = {
        revision.revision
        for revision in script.iterate_revisions(heads[0], "base")
    }
    assert REVISION in ancestry


def test_upgrade_adds_a_nullable_error_code_to_both_job_tables() -> None:
    recorder = _run("upgrade")

    for table in TABLES:
        assert ("add_column", table, "error_code") in recorder.calls
        column = recorder.added[f"{table}.error_code"]
        # NULL is the honest zero value: every existing row, and every
        # future ordinary crash, has no code the player can act on.
        assert column.nullable is True


def test_column_width_matches_the_service_clamp() -> None:
    recorder = _run("upgrade")

    for table in TABLES:
        column = recorder.added[f"{table}.error_code"]
        assert column.type.length == MAX_ERROR_CODE_CHARS


def test_downgrade_drops_both_columns() -> None:
    recorder = _run("downgrade")

    assert recorder.calls == [
        ("drop_column", "branching_dramas", "error_code"),
        ("drop_column", "fusion_stories", "error_code"),
    ]
