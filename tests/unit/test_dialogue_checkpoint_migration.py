"""Migration smoke for ``dialogue_checkpoints`` (v2j8d5g10047).

Recording-op pattern (see ``test_background_jobs_migration``): swap
alembic's ``op`` for a stub that records calls, run ``upgrade()`` and
``downgrade()``, and assert the shape.

What is worth asserting here beyond "a table appears":

* the primary key is the **pair**, not a surrogate id. A surrogate would
  let two live checkpoints exist for one conversation partner, and
  nothing downstream would notice until one of them started winning at
  random.
* the migration chains onto the head this ticket found, so the tree
  stays single-headed.
* the ORM model and the migration agree on the column set — two
  declarations of one table is how a column ends up existing on one
  deployment and not another.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

from kokoro_link.infrastructure.persistence.models import DialogueCheckpointRow

MIGRATION_PATH = (
    Path(__file__).parents[2]
    / "alembic"
    / "versions"
    / "v2j8d5g10047_dialogue_checkpoints.py"
)


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "dialogue_checkpoints_migration", MIGRATION_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _RecordingOp:
    def __init__(self) -> None:
        self.created_tables: list[tuple[str, list[str], list[Any]]] = []
        self.dropped_tables: list[str] = []

    def create_table(self, name: str, *elements: Any, **kwargs: Any) -> None:
        columns = [
            element.name for element in elements
            if hasattr(element, "name") and hasattr(element, "type")
        ]
        constraints = [
            element for element in elements
            if not (hasattr(element, "name") and hasattr(element, "type"))
        ]
        self.created_tables.append((name, columns, constraints))

    def drop_table(self, name: str) -> None:
        self.dropped_tables.append(name)


def _run() -> _RecordingOp:
    module = _load_migration()
    recorder = _RecordingOp()
    module.op = recorder  # type: ignore[attr-defined]
    module.upgrade()
    module.downgrade()
    return recorder


def test_the_revision_chains_onto_the_current_head() -> None:
    module = _load_migration()
    assert module.revision == "v2j8d5g10047"
    assert module.down_revision == "u1i7c4f10046"


def test_upgrade_creates_the_table_and_downgrade_drops_it() -> None:
    recorder = _run()
    assert [name for name, _, _ in recorder.created_tables] == [
        "dialogue_checkpoints",
    ]
    assert recorder.dropped_tables == ["dialogue_checkpoints"]


def test_the_primary_key_is_the_character_operator_pair() -> None:
    """No surrogate id: one pair, one checkpoint, enforced by the
    database rather than by every writer remembering to."""
    _, _, constraints = _run().created_tables[0]
    # ``_pending_colargs`` is where an unbound constraint keeps the
    # column *names* it was declared with; ``.columns`` is empty until
    # the constraint is attached to a real table, which never happens
    # under the recording op.
    primary_keys = [
        list(constraint._pending_colargs) for constraint in constraints
        if type(constraint).__name__ == "PrimaryKeyConstraint"
    ]
    assert primary_keys == [["character_id", "operator_id"]]


def test_the_character_foreign_key_cascades() -> None:
    _, _, constraints = _run().created_tables[0]
    foreign_keys = [
        constraint for constraint in constraints
        if type(constraint).__name__ == "ForeignKeyConstraint"
    ]
    assert len(foreign_keys) == 1
    assert list(foreign_keys[0]._pending_colargs) == ["character_id"]
    assert foreign_keys[0].ondelete == "CASCADE"


def test_the_migration_and_the_orm_model_declare_the_same_columns() -> None:
    _, columns, _ = _run().created_tables[0]
    assert set(columns) == {
        column.name for column in DialogueCheckpointRow.__table__.columns
    }
