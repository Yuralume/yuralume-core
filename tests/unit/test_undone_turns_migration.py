"""Real clean-SQLite upgrade for ``s9g5a2d10044`` (TU1 undo tombstones).

Same shape as ``test_story_scene_sessions_migration``: the DDL is run for
real against a fresh on-disk SQLite database rather than compared with
the ORM metadata, because the two can drift and only the migration is
what a deployment actually executes.

What is pinned is the invariant, not the column list:

* ``turn_record_id`` is the **primary key**. The undo records a
  tombstone without checking for one first, so idempotence is the
  database's job; a table built with a plain indexed column would let a
  second undo of the same turn raise and abort the rollback.
* The FK to ``conversations`` really cascades. Tombstones must outlive
  the *journal* (which the undo deletes as its last step) but not the
  conversation — without the cascade the table only ever grows in the
  one direction the GC sweep does not cover.

The full base→head chain is not runnable on SQLite (an earlier migration
issues ``CREATE EXTENSION vector``), so the FK target is seeded by hand
and the parent revision is stamped.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import IntegrityError

_REPO_ROOT = Path(__file__).parents[2].resolve()
_PARENT_REVISION = "r8f4z1c10043"
_REVISION = "s9g5a2d10044"


def _alembic_config(db_url: str) -> Config:
    # No alembic.ini on purpose: env.py would run ``fileConfig`` and
    # reconfigure logging with ``disable_existing_loggers=True``, silently
    # breaking every caplog-based test later in the same process.
    config = Config()
    config.set_main_option("script_location", str(_REPO_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", db_url)
    return config


def _seed_prerequisite_tables(engine) -> None:  # noqa: ANN001
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE conversations (id VARCHAR PRIMARY KEY)"))
        conn.execute(text("INSERT INTO conversations (id) VALUES ('conv-1')"))
        conn.execute(text("INSERT INTO conversations (id) VALUES ('conv-2')"))


def _insert_tombstone(
    conn,  # noqa: ANN001
    *,
    turn_record_id: str,
    conversation_id: str = "conv-1",
    undone_at: str = "2026-08-25 09:30:00",
) -> None:
    conn.execute(
        text(
            "INSERT INTO undone_turns (turn_record_id, conversation_id,"
            " undone_at) VALUES (:turn_record_id, :conversation_id, :undone_at)"
        ),
        {
            "turn_record_id": turn_record_id,
            "conversation_id": conversation_id,
            "undone_at": undone_at,
        },
    )


@pytest.fixture()
def sqlite_url(tmp_path) -> str:  # noqa: ANN001
    return f"sqlite:///{tmp_path / 'migration.db'}"


@pytest.fixture()
def engine(sqlite_url, monkeypatch):  # noqa: ANN001, ANN201
    # env.py resolves DATABASE_URL (and load_dotenv would otherwise re-inject
    # the dev Postgres URL from .env); pin it to the SQLite target.
    monkeypatch.setenv("DATABASE_URL", sqlite_url)
    monkeypatch.delenv("KOKORO_DATABASE_URL", raising=False)
    eng = create_engine(sqlite_url)

    # SQLite ignores foreign keys unless asked; without this the cascade
    # assertion below would pass on a table that has no working FK.
    @event.listens_for(eng, "connect")
    def _enable_foreign_keys(dbapi_connection, _record):  # noqa: ANN202
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    _seed_prerequisite_tables(eng)
    try:
        yield eng
    finally:
        eng.dispose()


def _upgrade(sqlite_url: str) -> None:
    config = _alembic_config(sqlite_url)
    command.stamp(config, _PARENT_REVISION)
    command.upgrade(config, _REVISION)


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


def test_turn_record_id_is_the_primary_key(engine, sqlite_url) -> None:  # noqa: ANN001
    """Recording is idempotent because the database says so.

    The undo writes a tombstone without looking for one first — a second
    undo of the same turn must collide, not duplicate."""
    _upgrade(sqlite_url)

    with engine.begin() as conn:
        _insert_tombstone(conn, turn_record_id="turn-1")

    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            _insert_tombstone(conn, turn_record_id="turn-1")

    with engine.connect() as conn:
        assert conn.execute(
            text("SELECT COUNT(*) FROM undone_turns"),
        ).scalar() == 1


def test_tombstones_die_with_their_conversation(engine, sqlite_url) -> None:  # noqa: ANN001
    """The row outlives the journal by design; it must not outlive the
    conversation, or the only thing bounding the table is the GC sweep."""
    _upgrade(sqlite_url)

    with engine.begin() as conn:
        _insert_tombstone(conn, turn_record_id="turn-1")
        _insert_tombstone(conn, turn_record_id="turn-2")
        _insert_tombstone(
            conn, turn_record_id="turn-3", conversation_id="conv-2",
        )

    with engine.begin() as conn:
        conn.execute(text("DELETE FROM conversations WHERE id = 'conv-1'"))

    with engine.connect() as conn:
        survivors = [
            row[0] for row in conn.execute(
                text("SELECT turn_record_id FROM undone_turns"),
            )
        ]
    assert survivors == ["turn-3"]


def test_a_tombstone_needs_a_real_conversation(engine, sqlite_url) -> None:  # noqa: ANN001
    """Guard the guard: if this passes, the FK in the previous test was
    doing nothing and the cascade assertion was vacuous."""
    _upgrade(sqlite_url)

    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            _insert_tombstone(
                conn, turn_record_id="turn-1", conversation_id="ghost",
            )


def test_gc_sweep_can_select_by_age(engine, sqlite_url) -> None:  # noqa: ANN001
    """``undone_at`` is the sweep's only predicate, and it is indexed for
    exactly that reason."""
    _upgrade(sqlite_url)

    with engine.begin() as conn:
        _insert_tombstone(
            conn, turn_record_id="old", undone_at="2026-08-01 00:00:00",
        )
        _insert_tombstone(
            conn, turn_record_id="fresh", undone_at="2026-08-25 09:30:00",
        )
        conn.execute(
            text("DELETE FROM undone_turns WHERE undone_at < :cutoff"),
            {"cutoff": "2026-08-20 00:00:00"},
        )

    with engine.connect() as conn:
        survivors = [
            row[0] for row in conn.execute(
                text("SELECT turn_record_id FROM undone_turns"),
            )
        ]
        indexes = {
            row[0] for row in conn.execute(
                text(
                    "SELECT name FROM sqlite_master WHERE type = 'index'"
                    " AND tbl_name = 'undone_turns'"
                ),
            )
        }
    assert survivors == ["fresh"]
    assert "ix_undone_turns_undone_at" in indexes


def test_downgrade_drops_the_table(engine, sqlite_url) -> None:  # noqa: ANN001
    config = _alembic_config(sqlite_url)
    command.stamp(config, _PARENT_REVISION)
    command.upgrade(config, _REVISION)
    command.downgrade(config, _PARENT_REVISION)

    with engine.connect() as conn:
        tables = {
            row[0] for row in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type = 'table'"),
            )
        }
    assert "undone_turns" not in tables
