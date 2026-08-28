"""Migration smoke for ``prompt_material_digests`` (d8w4o2l10053).

Recording-op pattern (see ``test_dialogue_checkpoint_migration``): swap
alembic's ``op`` for a stub that records calls, run ``upgrade()`` and
``downgrade()``, and assert the shape.

What is worth asserting beyond "a table appears":

* the primary key is the **pair**, not a surrogate id. A surrogate would
  let two live digests exist for one relationship, and the reader would
  start serving whichever it happened to find.
* the character FK cascades, so deleting a character takes its budgeted
  digests with it. This table is deliberately *not* in the character
  backup registry — every column is rebuildable by one post-turn — so the
  cascade is the only thing standing between a delete and an orphan row.
* the migration chains onto the head this ticket found, so the main
  lineage stays single-headed.
* the ORM model and the migration agree on the column set — two
  declarations of one table is how a column ends up existing on one
  deployment and not another.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

from kokoro_link.infrastructure.persistence.models import (
    PromptMaterialDigestRow,
)

MIGRATION_PATH = (
    Path(__file__).parents[2]
    / "alembic"
    / "versions"
    / "d8w4o2l10053_prompt_material_digests.py"
)


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "prompt_material_digests_migration", MIGRATION_PATH,
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
    assert module.revision == "d8w4o2l10053"
    assert module.down_revision == "c7v3n1k10052"


def test_upgrade_creates_the_table_and_downgrade_drops_it() -> None:
    recorder = _run()
    assert [name for name, _, _ in recorder.created_tables] == [
        "prompt_material_digests",
    ]
    assert recorder.dropped_tables == ["prompt_material_digests"]


def test_the_primary_key_is_the_character_operator_pair() -> None:
    """No surrogate id: one pair, one digest, enforced by the database
    rather than by every writer remembering to."""
    _, _, constraints = _run().created_tables[0]
    # ``_pending_colargs`` is where an unbound constraint keeps the column
    # *names* it was declared with; ``.columns`` is empty until the
    # constraint is attached to a real table, which never happens under
    # the recording op.
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
        column.name for column in PromptMaterialDigestRow.__table__.columns
    }


def test_the_row_carries_what_bounds_a_read() -> None:
    """Both refusal grounds have a column, or the read cannot refuse.

    ``content_tolerance`` is what stops an NSFW-mode digest reaching a
    normal-mode prompt; ``updated_at`` is what stops a month-old summary
    being rendered as current. Neither is derivable from the bullets.
    """
    _, columns, _ = _run().created_tables[0]
    assert {"content_tolerance", "updated_at", "digest_json"} <= set(columns)
