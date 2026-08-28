"""Real clean-SQLite upgrade for ``t0h6b3e10045`` (TU4/TU6 turn anchors).

Same shape as ``test_undone_turns_migration``: the DDL runs for real
against a fresh on-disk SQLite database rather than being compared with
the ORM metadata, because the two drift and only the migration is what a
deployment executes.

What is pinned is the invariant, not the column list:

* the column is **nullable**, and rows that predate it stay readable.
  Undo's whole rolling-deployment story is "anchorless rows keep the old
  behaviour"; a ``NOT NULL`` column would have made the upgrade itself
  fail on any table with rows in it.
* the anchor is **indexed** on both tables. Undo's delete pass is one
  ``WHERE turn_record_id = ?`` per table, against tables whose purpose is
  to accumulate rows between releases.
* ``downgrade`` really removes both columns, so the revision is
  reversible on a rollback rather than only on paper.

The full base→head chain is not runnable on SQLite (an earlier migration
issues ``CREATE EXTENSION vector``), so the two tables are created by
hand in the shape the prior head leaves them and the parent revision is
stamped.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text

_REPO_ROOT = Path(__file__).parents[2].resolve()
_PARENT_REVISION = "s9g5a2d10044"
_REVISION = "t0h6b3e10045"

_ANCHORED_TABLES: tuple[tuple[str, str], ...] = (
    ("pending_follow_ups", "ix_pending_follow_ups_turn_record_id"),
    (
        "character_encounter_intents",
        "ix_character_encounter_intents_turn_record_id",
    ),
)


def _alembic_config(db_url: str) -> Config:
    # No alembic.ini on purpose: env.py would run ``fileConfig`` and
    # reconfigure logging with ``disable_existing_loggers=True``, silently
    # breaking every caplog-based test later in the same process.
    config = Config()
    config.set_main_option("script_location", str(_REPO_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", db_url)
    return config


def _seed_prerequisite_tables(engine) -> None:  # noqa: ANN001
    """The two tables as the prior head leaves them, plus one row each.

    The pre-existing rows are the point: they are what a real deployment
    has, and they are how "the column had better be nullable" is tested
    rather than asserted.
    """
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE pending_follow_ups ("
            " id VARCHAR PRIMARY KEY,"
            " character_id VARCHAR NOT NULL,"
            " conversation_id VARCHAR NOT NULL,"
            " status VARCHAR NOT NULL,"
            " kind VARCHAR NOT NULL DEFAULT 'busy_defer',"
            " queued_at TIMESTAMP NOT NULL)"
        ))
        conn.execute(text(
            "INSERT INTO pending_follow_ups"
            " (id, character_id, conversation_id, status, kind, queued_at)"
            " VALUES ('fu-legacy', 'char-1', 'conv-1', 'queued',"
            " 'scheduled_promise', '2026-08-24 10:00:00')"
        ))
        conn.execute(text(
            "CREATE TABLE character_encounter_intents ("
            " id VARCHAR PRIMARY KEY,"
            " character_id VARCHAR NOT NULL,"
            " peer_character_id VARCHAR NOT NULL,"
            " status VARCHAR NOT NULL,"
            " created_at TIMESTAMP NOT NULL)"
        ))
        conn.execute(text(
            "INSERT INTO character_encounter_intents"
            " (id, character_id, peer_character_id, status, created_at)"
            " VALUES ('int-legacy', 'char-1', 'char-2', 'pending',"
            " '2026-08-24 10:00:00')"
        ))


@pytest.fixture()
def sqlite_url(tmp_path) -> str:  # noqa: ANN001
    return f"sqlite:///{tmp_path / 'migration.db'}"


@pytest.fixture()
def engine(sqlite_url, monkeypatch):  # noqa: ANN001, ANN201
    # env.py resolves DATABASE_URL (and load_dotenv would otherwise
    # re-inject the dev Postgres URL from .env); pin it to SQLite.
    monkeypatch.setenv("DATABASE_URL", sqlite_url)
    monkeypatch.delenv("KOKORO_DATABASE_URL", raising=False)
    eng = create_engine(sqlite_url)
    _seed_prerequisite_tables(eng)
    try:
        yield eng
    finally:
        eng.dispose()


def _upgrade(sqlite_url: str) -> None:
    config = _alembic_config(sqlite_url)
    command.stamp(config, _PARENT_REVISION)
    command.upgrade(config, _REVISION)


def _columns(conn, table: str) -> set[str]:  # noqa: ANN001
    return {
        row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))
    }


def _indexes(conn, table: str) -> set[str]:  # noqa: ANN001
    return {
        row[0] for row in conn.execute(
            text(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
                " AND tbl_name = :table"
            ),
            {"table": table},
        )
    }


def test_revision_chains_to_prior_head_and_graph_stays_linear() -> None:
    config = Config(str(_REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_REPO_ROOT / "alembic"))
    script = ScriptDirectory.from_config(config)

    # Deliberately not ``heads == [_REVISION]``: this migration is only
    # the head until the next one chains onto it, and pinning it that way
    # would make an unrelated schema change look like a regression here.
    heads = list(script.get_heads())
    assert len(heads) == 1
    ancestry = {
        entry.revision
        for entry in script.iterate_revisions(heads[0], "base")
    }
    assert _REVISION in ancestry
    revision = script.get_revision(_REVISION)
    assert revision.down_revision == _PARENT_REVISION


def test_both_tables_gain_an_indexed_anchor(engine, sqlite_url) -> None:  # noqa: ANN001
    _upgrade(sqlite_url)

    with engine.connect() as conn:
        for table, index_name in _ANCHORED_TABLES:
            assert "turn_record_id" in _columns(conn, table), table
            assert index_name in _indexes(conn, table), table


def test_existing_rows_survive_with_a_null_anchor(engine, sqlite_url) -> None:  # noqa: ANN001
    """A ``NOT NULL`` column would have failed the upgrade outright on any
    populated table; a defaulted one would have invented an anchor that
    names no turn. ``NULL`` is what undo reads as "not mine to claim"."""
    _upgrade(sqlite_url)

    with engine.connect() as conn:
        assert conn.execute(text(
            "SELECT turn_record_id FROM pending_follow_ups WHERE id = 'fu-legacy'"
        )).scalar() is None
        assert conn.execute(text(
            "SELECT turn_record_id FROM character_encounter_intents"
            " WHERE id = 'int-legacy'"
        )).scalar() is None


def test_the_anchor_is_writable_and_selectable(engine, sqlite_url) -> None:  # noqa: ANN001
    """Guard on the two above: the column must actually carry the value
    undo looks rows up by, not merely exist."""
    _upgrade(sqlite_url)

    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO pending_follow_ups"
            " (id, character_id, conversation_id, status, kind, queued_at,"
            " turn_record_id)"
            " VALUES ('fu-new', 'char-1', 'conv-1', 'queued',"
            " 'scheduled_promise', '2026-08-25 10:00:00', 'turn-9')"
        ))
        conn.execute(text(
            "INSERT INTO character_encounter_intents"
            " (id, character_id, peer_character_id, status, created_at,"
            " turn_record_id)"
            " VALUES ('int-new', 'char-1', 'char-2', 'pending',"
            " '2026-08-25 10:00:00', 'turn-9')"
        ))

    with engine.connect() as conn:
        assert [
            row[0] for row in conn.execute(text(
                "SELECT id FROM pending_follow_ups"
                " WHERE turn_record_id = 'turn-9'"
            ))
        ] == ["fu-new"]
        assert [
            row[0] for row in conn.execute(text(
                "SELECT id FROM character_encounter_intents"
                " WHERE turn_record_id = 'turn-9'"
            ))
        ] == ["int-new"]


def test_downgrade_removes_both_columns(engine, sqlite_url) -> None:  # noqa: ANN001
    config = _alembic_config(sqlite_url)
    command.stamp(config, _PARENT_REVISION)
    command.upgrade(config, _REVISION)
    command.downgrade(config, _PARENT_REVISION)

    with engine.connect() as conn:
        for table, index_name in _ANCHORED_TABLES:
            assert "turn_record_id" not in _columns(conn, table), table
            assert index_name not in _indexes(conn, table), table
        # And the rows that were there before the upgrade are still there.
        assert conn.execute(
            text("SELECT COUNT(*) FROM pending_follow_ups"),
        ).scalar() == 1
        assert conn.execute(
            text("SELECT COUNT(*) FROM character_encounter_intents"),
        ).scalar() == 1
