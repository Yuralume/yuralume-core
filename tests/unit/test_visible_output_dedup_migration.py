"""Migration smoke for the visible-output dedup constraints (j5h2f7g90911).

Recording-op pattern (see test_background_jobs_migration): swap the alembic
``op`` for a stub that records calls in order, run upgrade()/downgrade(), and
assert (a) the additive slot table + its unique claim index, and (b) that the
daily_schedules data-dedup DELETE runs BEFORE the unique constraint is added
(adding it first would fail on any legacy duplicate row).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any


MIGRATION_PATH = (
    Path(__file__).parents[1]
    / ".."
    / "alembic"
    / "versions"
    / "j5h2f7g90911_visible_output_dedup.py"
).resolve()


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "test_visible_output_dedup_migration_module", MIGRATION_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _RecordingOp:
    def __init__(self) -> None:
        # A single ordered ledger of ("op", name) so ordering assertions are
        # easy across the different alembic verbs the migration mixes.
        self.calls: list[tuple[str, str]] = []
        self.created_tables: list[str] = []
        self.dropped_tables: list[str] = []
        self.created_indexes: list[str] = []
        self.created_unique: list[str] = []

    def execute(self, statement: Any) -> None:
        self.calls.append(("execute", str(statement)[:40]))

    def create_unique_constraint(
        self, name: str, table: str, columns: Any, **kwargs: Any,
    ) -> None:
        self.calls.append(("create_unique_constraint", name))
        self.created_unique.append(name)

    def create_table(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append(("create_table", name))
        self.created_tables.append(name)
        # Capture inline constraint names so the claim UNIQUE (declared inside
        # create_table, not as a separate create_unique_constraint) is visible.
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

    def drop_constraint(self, name: str, table: str, **kwargs: Any) -> None:
        self.calls.append(("drop_constraint", name))


def test_revision_chains_to_prior_head() -> None:
    migration = _load_migration()
    assert migration.revision == "j5h2f7g90911"
    assert migration.down_revision == "i4g1e6f80810"


def test_upgrade_creates_slot_table_and_claim_unique(monkeypatch) -> None:  # noqa: ANN001
    migration = _load_migration()
    recorder = _RecordingOp()
    monkeypatch.setattr(migration, "op", recorder)

    migration.upgrade()

    assert recorder.created_tables == ["background_visible_slots"]
    assert (
        "uq_background_visible_slots_claim" in recorder.created_unique
    )
    assert (
        "ix_background_visible_slots_created" in recorder.created_indexes
    )


def test_upgrade_dedups_before_adding_schedule_unique(monkeypatch) -> None:  # noqa: ANN001
    migration = _load_migration()
    recorder = _RecordingOp()
    monkeypatch.setattr(migration, "op", recorder)

    migration.upgrade()

    verbs = [verb for verb, _ in recorder.calls]
    dedup_index = verbs.index("execute")
    unique_index = next(
        i for i, (verb, name) in enumerate(recorder.calls)
        if verb == "create_unique_constraint"
        and name == "uq_daily_schedules_character_date"
    )
    # The data-dedup DELETE must precede the constraint add.
    assert dedup_index < unique_index


def test_downgrade_drops_slot_table_and_schedule_unique(monkeypatch) -> None:  # noqa: ANN001
    migration = _load_migration()
    recorder = _RecordingOp()
    monkeypatch.setattr(migration, "op", recorder)

    migration.downgrade()

    assert recorder.dropped_tables == ["background_visible_slots"]
    assert (
        "drop_constraint",
        "uq_daily_schedules_character_date",
    ) in recorder.calls
