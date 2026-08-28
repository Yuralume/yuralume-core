"""Migration smoke for ``pending_follow_ups.honesty_park_attempts`` (w3k9e6h10048).

Recording-op pattern (see test_daily_schedule_manually_adjusted_migration):
swap the alembic ``op`` for a stub that records calls, run
upgrade()/downgrade(), and assert the single additive column.

The backfill matters more than usual here: this column is a *budget*, and
``server_default='0'`` is what gives every row that already exists at
upgrade time a full untouched allowance. A default of anything else would
retroactively spend part of a live promise's retries.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

from alembic.config import Config
from alembic.script import ScriptDirectory


REVISION = "w3k9e6h10048"
DOWN_REVISION = "v2j8d5g10047"

REPO_ROOT = Path(__file__).parents[2].resolve()
MIGRATION_PATH = (
    REPO_ROOT / "alembic" / "versions"
    / f"{REVISION}_pending_follow_up_honesty_park_attempts.py"
)

TABLE = "pending_follow_ups"
COLUMN = "honesty_park_attempts"


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "test_pending_follow_up_honesty_park_attempts_migration_module",
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


def test_upgrade_adds_a_non_null_counter_backfilled_to_zero() -> None:
    recorder = _run("upgrade")

    assert ("add_column", TABLE, COLUMN) in recorder.calls
    column = recorder.added[f"{TABLE}.{COLUMN}"]
    assert column.nullable is False
    assert column.server_default is not None
    assert str(column.server_default.arg).strip() == "0"


def test_downgrade_drops_the_column() -> None:
    recorder = _run("downgrade")
    assert ("drop_column", TABLE, COLUMN) in recorder.calls


def test_the_orm_model_and_the_migration_agree() -> None:
    """The column the migration creates is the column the mapper reads.

    Cheap, and it is the pairing that silently rots: an ORM attribute
    added without the migration passes every unit test that uses the
    in-memory repository and fails only against a real database."""
    from kokoro_link.infrastructure.persistence.models import PendingFollowUpRow

    column = PendingFollowUpRow.__table__.columns[COLUMN]
    assert column.nullable is False
    assert str(column.server_default.arg).strip() == "0"
