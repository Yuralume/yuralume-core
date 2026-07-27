"""Migration smoke for the inbound receipt ledger (u6s3t8v9022).

Recording-op pattern (see test_visible_output_dedup_migration): swap the
alembic ``op`` for a stub that records calls in order, run
upgrade()/downgrade(), and assert the additive table, its unique claim
constraint, and the retention index. Also pins that the revision graph still
has exactly one head — parallel branches would break ``alembic upgrade head``
on deploy.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

from alembic.config import Config
from alembic.script import ScriptDirectory


REVISION = "u6s3t8v9022"
DOWN_REVISION = "t5r2s7u8021"

REPO_ROOT = Path(__file__).parents[2].resolve()
MIGRATION_PATH = (
    REPO_ROOT / "alembic" / "versions" / f"{REVISION}_inbound_message_receipts.py"
)


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "test_inbound_message_receipts_migration_module", MIGRATION_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _RecordingOp:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.created_tables: list[str] = []
        self.dropped_tables: list[str] = []
        self.created_indexes: list[str] = []
        self.created_unique: list[str] = []

    def create_table(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append(("create_table", name))
        self.created_tables.append(name)
        for arg in args:
            constraint_name = getattr(arg, "name", None)
            if constraint_name:
                self.created_unique.append(constraint_name)

    def create_index(
        self, name: str, table: str, columns: Any, **kwargs: Any,
    ) -> None:
        self.calls.append(("create_index", name))
        self.created_indexes.append(name)

    def drop_index(self, name: str, **kwargs: Any) -> None:
        self.calls.append(("drop_index", name))

    def drop_table(self, name: str) -> None:
        self.calls.append(("drop_table", name))
        self.dropped_tables.append(name)


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


def test_upgrade_creates_receipt_table_with_claim_unique(monkeypatch) -> None:  # noqa: ANN001
    migration = _load_migration()
    recorder = _RecordingOp()
    monkeypatch.setattr(migration, "op", recorder)

    migration.upgrade()

    assert recorder.created_tables == ["inbound_message_receipts"]
    assert "uq_inbound_message_receipts_delivery" in recorder.created_unique
    assert "ix_inbound_message_receipts_created" in recorder.created_indexes


def test_downgrade_drops_receipt_table(monkeypatch) -> None:  # noqa: ANN001
    migration = _load_migration()
    recorder = _RecordingOp()
    monkeypatch.setattr(migration, "op", recorder)

    migration.downgrade()

    assert recorder.dropped_tables == ["inbound_message_receipts"]
    assert ("drop_index", "ix_inbound_message_receipts_created") in recorder.calls
