"""Migration smoke for ``player_identity_cards`` (IC1).

Beyond the usual create/drop symmetry, this pins the two shape decisions
the plan calls out: no character reference anywhere in the table, and a
per-operator unique name.

The recorded ``create_table`` arguments are reassembled into a real
``sa.Table`` before being inspected — a bare ``UniqueConstraint`` has not
resolved its column names yet, so asserting against the raw arguments
would silently pass on an empty tuple.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

import sqlalchemy as sa
from alembic.config import Config
from alembic.script import ScriptDirectory

from kokoro_link.domain.entities.player_identity_card import (
    PLAYER_IDENTITY_CARD_CONTENT_FIELDS,
)


REPO_ROOT = Path(__file__).parents[2].resolve()

MIGRATION_PATH = (
    Path(__file__).parents[1]
    / ".."
    / "alembic"
    / "versions"
    / "c7v3n1k10052_player_identity_cards.py"
).resolve()


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "player_identity_cards", MIGRATION_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _RecordingOp:
    def __init__(self) -> None:
        self.created: list[tuple[str, tuple[Any, ...]]] = []
        self.dropped: list[str] = []
        self.indexes: list[tuple[str, str, list[str]]] = []
        self.dropped_indexes: list[str] = []

    def create_table(self, name: str, *columns: Any, **kwargs: Any) -> None:
        self.created.append((name, columns))

    def drop_table(self, name: str) -> None:
        self.dropped.append(name)

    def create_index(
        self, name: str, table_name: str, columns: list[str], **kwargs: Any,
    ) -> None:
        self.indexes.append((name, table_name, columns))

    def drop_index(self, name: str, **kwargs: Any) -> None:
        self.dropped_indexes.append(name)


def _upgraded() -> _RecordingOp:
    migration = _load_migration()
    recorder = _RecordingOp()
    migration.op = recorder  # type: ignore[attr-defined]
    migration.upgrade()
    return recorder


def _created_table(recorder: _RecordingOp) -> sa.Table:
    name, args = recorder.created[0]
    return sa.Table(name, sa.MetaData(), *args)


def test_revision_chains_to_the_player_knowledge_head() -> None:
    migration = _load_migration()

    assert migration.revision == "c7v3n1k10052"
    assert migration.down_revision == "z5s2n9q80051"


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
    assert "c7v3n1k10052" in ancestry


def test_upgrade_creates_the_operator_scoped_table() -> None:
    recorder = _upgraded()

    assert [name for name, _ in recorder.created] == ["player_identity_cards"]
    table = _created_table(recorder)
    assert list(table.primary_key.columns.keys()) == ["id"]
    assert recorder.indexes == [
        (
            "ix_player_identity_cards_operator",
            "player_identity_cards",
            ["operator_id", "updated_at"],
        ),
    ]


def test_table_carries_the_whole_card_content() -> None:
    columns = set(_created_table(_upgraded()).columns.keys())

    assert {"id", "operator_id", "name", "created_at", "updated_at"} <= columns
    assert set(PLAYER_IDENTITY_CARD_CONTENT_FIELDS) <= columns


def test_no_character_reference_column() -> None:
    """A card is operator-level.

    A ``character_id`` here would both misstate the card's lifetime and
    enrol the player's whole card library into one character's
    ``.lumebackup`` (see ``tests/unit/test_character_backup_registry``).
    """
    offenders = [
        name for name in _created_table(_upgraded()).columns.keys()
        if "character" in name and name.endswith(("_id", "_ids_json"))
    ]

    assert offenders == []


def test_card_names_are_unique_per_operator() -> None:
    table = _created_table(_upgraded())

    unique_pairs = [
        tuple(constraint.columns.keys())
        for constraint in table.constraints
        if isinstance(constraint, sa.UniqueConstraint)
    ]

    assert ("operator_id", "name") in unique_pairs


def test_operator_id_cascades_from_the_owner_account() -> None:
    table = _created_table(_upgraded())

    # ``_colspec`` rather than ``key.column``: the referenced table is not
    # in this throwaway MetaData, so resolving the target would raise.
    targets = {
        key._colspec
        for constraint in table.foreign_key_constraints
        for key in constraint.elements
    }
    ondelete = {
        constraint.ondelete for constraint in table.foreign_key_constraints
    }

    assert targets == {"operator_profiles.id"}
    assert ondelete == {"CASCADE"}


def test_downgrade_drops_the_index_then_the_table() -> None:
    migration = _load_migration()
    recorder = _RecordingOp()
    migration.op = recorder  # type: ignore[attr-defined]

    migration.downgrade()

    assert recorder.dropped == ["player_identity_cards"]
    assert recorder.dropped_indexes == ["ix_player_identity_cards_operator"]
