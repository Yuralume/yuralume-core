"""Migration smoke for the LR campaign ledger (LR T1).

Beyond create/drop symmetry this pins the two shape decisions the plan
rests on: the item table's primary key is the ``(campaign_id,
character_id)`` pair — which is what makes per-item idempotency a schema
property rather than a runner convention — and the outcome columns are
nullable, because ``outcome IS NULL`` *is* the pending marker a resume
selects on.

The recorded ``create_table`` arguments are reassembled into a real
``sa.Table`` before inspection: a bare constraint has not resolved its
column names yet, so asserting against the raw arguments would silently
pass on an empty tuple.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

import sqlalchemy as sa
from alembic.config import Config
from alembic.script import ScriptDirectory


REPO_ROOT = Path(__file__).parents[2].resolve()

MIGRATION_PATH = (
    REPO_ROOT
    / "alembic"
    / "versions"
    / "e9x5p3m10054_line_reactivation_campaigns.py"
).resolve()

_CAMPAIGNS = "line_reactivation_campaigns"
_ITEMS = "line_reactivation_campaign_items"


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "line_reactivation_campaigns", MIGRATION_PATH,
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


def _created_table(recorder: _RecordingOp, name: str) -> sa.Table:
    for created_name, args in recorder.created:
        if created_name == name:
            return sa.Table(created_name, sa.MetaData(), *args)
    raise AssertionError(f"{name} was not created")


def test_revision_chains_to_the_material_digest_head() -> None:
    migration = _load_migration()

    assert migration.revision == "e9x5p3m10054"
    assert migration.down_revision == "d8w4o2l10053"


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
    assert "e9x5p3m10054" in ancestry


def test_upgrade_creates_both_ledger_tables() -> None:
    recorder = _upgraded()

    assert [name for name, _ in recorder.created] == [_CAMPAIGNS, _ITEMS]


def test_campaign_is_keyed_on_the_console_supplied_id() -> None:
    table = _created_table(_upgraded(), _CAMPAIGNS)

    assert list(table.primary_key.columns.keys()) == ["campaign_id"]
    assert set(table.columns.keys()) == {
        "campaign_id", "actor", "status", "created_at", "completed_at", "total",
    }


def test_campaign_table_has_no_character_reference() -> None:
    """The campaign row is the operator's action, not a character's data.

    A ``character_id`` here would drag the whole ledger into the
    character-backup boundary scan (see
    ``tests/unit/test_character_backup_registry``) and misstate the row's
    lifetime — it outlives every character it names.
    """
    offenders = [
        name
        for name in _created_table(_upgraded(), _CAMPAIGNS).columns.keys()
        if "character" in name
    ]

    assert offenders == []


def test_item_primary_key_is_the_campaign_character_pair() -> None:
    table = _created_table(_upgraded(), _ITEMS)

    assert list(table.primary_key.columns.keys()) == [
        "campaign_id", "character_id",
    ]
    assert set(table.columns.keys()) == {
        "campaign_id",
        "character_id",
        "outcome",
        "detail",
        "message_text",
        "attempted_at",
        "claimed_at",
    }


def test_item_outcome_columns_are_nullable_because_null_means_pending() -> None:
    table = _created_table(_upgraded(), _ITEMS)

    assert table.columns["outcome"].nullable is True
    assert table.columns["detail"].nullable is True
    assert table.columns["attempted_at"].nullable is True


def test_item_carries_the_sent_message_verbatim() -> None:
    """The report column, added in place 2026-08-28 (G1).

    ``TEXT`` and not a bounded string on purpose: the operator's whole
    workflow is to read what a small batch of characters actually said
    before releasing the rest, and a column that silently clips the body
    would have them judging a message that ends mid-thought. Nullable
    because only a ``sent`` row has one.
    """
    table = _created_table(_upgraded(), _ITEMS)

    column = table.columns["message_text"]

    assert column.nullable is True
    assert isinstance(column.type, sa.Text)


def test_item_carries_the_cross_replica_claim_lease() -> None:
    """Without ``claimed_at`` the primary key only fences the *write*.

    Two API replicas handed the same campaign would both evaluate a
    pending row and only collide at ``record_outcome`` — after the second
    message is already in front of the player. The claim column is what
    lets a runner take the row before it dispatches, and it must be
    nullable because an unclaimed row is the normal state.
    """
    table = _created_table(_upgraded(), _ITEMS)

    assert table.columns["claimed_at"].nullable is True


def test_item_cascades_from_both_parents() -> None:
    table = _created_table(_upgraded(), _ITEMS)

    # ``_colspec`` rather than ``key.column``: the referenced tables are
    # not in this throwaway MetaData, so resolving them would raise.
    targets = {
        key._colspec
        for constraint in table.foreign_key_constraints
        for key in constraint.elements
    }
    ondelete = {
        constraint.ondelete for constraint in table.foreign_key_constraints
    }

    assert targets == {
        f"{_CAMPAIGNS}.campaign_id", "characters.id",
    }
    assert ondelete == {"CASCADE"}


def test_pending_lookup_is_indexed() -> None:
    recorder = _upgraded()

    assert (
        "ix_line_reactivation_campaign_items_pending",
        _ITEMS,
        ["campaign_id", "outcome"],
    ) in recorder.indexes


def test_downgrade_drops_children_before_parents() -> None:
    migration = _load_migration()
    recorder = _RecordingOp()
    migration.op = recorder  # type: ignore[attr-defined]

    migration.downgrade()

    assert recorder.dropped == [_ITEMS, _CAMPAIGNS]
    assert recorder.dropped_indexes == [
        "ix_line_reactivation_campaign_items_pending",
        "ix_line_reactivation_campaigns_created",
    ]
