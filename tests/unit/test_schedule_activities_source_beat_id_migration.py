"""Migration smoke for ``schedule_activities.source_beat_id`` (KB2).

Recording-op pattern (see test_feed_posts_viewed_at_migration /
test_daily_schedule_manually_adjusted_migration): swap the alembic ``op``
for a stub that records calls, run upgrade()/downgrade(), and assert the
single additive, nullable, zero-backfill column — ``NULL`` means "an
ordinary activity", which is the honest reading of every pre-migration
row (D7 of PLAYER_KNOWLEDGE_BOUNDARY_PLAN).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

from alembic.config import Config
from alembic.script import ScriptDirectory


REVISION = "y5m1g8j10050"
DOWN_REVISION = "x4l0f7i10049"

REPO_ROOT = Path(__file__).parents[2].resolve()
MIGRATION_PATH = (
    REPO_ROOT / "alembic" / "versions"
    / f"{REVISION}_schedule_activities_source_beat_id.py"
)

TABLE = "schedule_activities"
COLUMN = "source_beat_id"


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "test_schedule_activities_source_beat_id_migration_module",
        MIGRATION_PATH,
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


def test_upgrade_adds_a_nullable_column_with_zero_backfill() -> None:
    recorder = _run("upgrade")

    assert ("add_column", TABLE, COLUMN) in recorder.calls
    column = recorder.added[f"{TABLE}.{COLUMN}"]
    assert column.nullable is True
    # No server_default: existing blocks land at NULL ("no beat lineage
    # recorded"), which is what they were — not a placeholder guess, and
    # certainly not one inferred from the activity's text.
    assert column.server_default is None


def test_downgrade_drops_the_column() -> None:
    recorder = _run("downgrade")
    assert ("drop_column", TABLE, COLUMN) in recorder.calls


def test_the_orm_model_and_the_migration_agree() -> None:
    """The column the migration creates is the column the mapper reads.

    Cheap, and it is the pairing that silently rots: an ORM attribute
    added without the migration passes every unit test that uses the
    in-memory repository and fails only against a real database."""
    from kokoro_link.infrastructure.persistence.models import (
        ScheduleActivityRow,
    )

    column = ScheduleActivityRow.__table__.columns[COLUMN]
    assert column.nullable is True
    assert column.server_default is None
