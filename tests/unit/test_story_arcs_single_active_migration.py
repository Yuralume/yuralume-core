"""Real clean-SQLite upgrade for ``s4q1r6t7020`` + ``t5r2s7u8021``.

The bug that motivated the partial unique index was live in production, so the
upgrade has to survive a database that *already* contains two active arcs for
one character. This runs the real migration DDL against a fresh on-disk SQLite
database — the same shape as ``test_character_image_candidate_batches_migration``
— with duplicates planted first, and asserts both the convergence and that the
index really rejects a subsequent duplicate.

The full base→head chain is not runnable on SQLite (an earlier migration issues
``CREATE EXTENSION vector``), so the prerequisite tables are seeded by hand and
the parent revision is stamped.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

_REPO_ROOT = Path(__file__).parents[2]
_PARENT_REVISION = "r3p0q5s6019"
_ARC_REVISION = "s4q1r6t7020"
_CONSOLIDATION_REVISION = "t5r2s7u8021"
_INDEX = "uq_story_arcs_active_character"


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
        conn.execute(
            text(
                "CREATE TABLE characters ("
                "  id VARCHAR PRIMARY KEY,"
                "  name VARCHAR"
                ")"
            ),
        )
        conn.execute(
            text(
                "CREATE TABLE story_arcs ("
                "  id VARCHAR PRIMARY KEY,"
                "  character_id VARCHAR NOT NULL,"
                "  title TEXT NOT NULL,"
                "  premise TEXT NOT NULL,"
                "  theme VARCHAR NOT NULL,"
                "  tone VARCHAR NOT NULL,"
                "  source_template_id VARCHAR,"
                "  start_date VARCHAR NOT NULL,"
                "  end_date VARCHAR NOT NULL,"
                "  status VARCHAR NOT NULL,"
                "  created_at TIMESTAMP NOT NULL,"
                "  updated_at TIMESTAMP NOT NULL"
                ")"
            ),
        )


def _insert_arc(
    conn,  # noqa: ANN001
    *,
    arc_id: str,
    character_id: str,
    status: str,
    updated_at: str,
) -> None:
    conn.execute(
        text(
            "INSERT INTO story_arcs (id, character_id, title, premise, theme,"
            " tone, source_template_id, start_date, end_date, status,"
            " created_at, updated_at)"
            " VALUES (:id, :cid, 't', 'p', 'custom', 'daily', NULL,"
            " '2026-04-01', '2026-04-21', :status,"
            " '2026-04-01 00:00:00', :updated)"
        ),
        {
            "id": arc_id,
            "cid": character_id,
            "status": status,
            "updated": updated_at,
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
    _seed_prerequisite_tables(eng)
    try:
        yield eng
    finally:
        eng.dispose()


def test_upgrade_converges_pre_existing_duplicate_active_arcs(
    engine, sqlite_url,
) -> None:  # noqa: ANN001
    with engine.begin() as conn:
        # The exact production shape: two replicas each planned a full arc.
        _insert_arc(
            conn, arc_id="loser", character_id="c1",
            status="active", updated_at="2026-04-10 09:00:00",
        )
        _insert_arc(
            conn, arc_id="winner", character_id="c1",
            status="active", updated_at="2026-04-10 10:00:00",
        )
        # Untouched neighbours: another character, and terminal history.
        _insert_arc(
            conn, arc_id="other-active", character_id="c2",
            status="active", updated_at="2026-04-10 10:00:00",
        )
        _insert_arc(
            conn, arc_id="old", character_id="c1",
            status="completed", updated_at="2026-03-01 10:00:00",
        )
        _insert_arc(
            conn, arc_id="dropped", character_id="c1",
            status="abandoned", updated_at="2026-03-05 10:00:00",
        )

    config = _alembic_config(sqlite_url)
    command.stamp(config, _PARENT_REVISION)
    command.upgrade(config, _ARC_REVISION)

    with engine.connect() as conn:
        statuses = dict(
            conn.execute(text("SELECT id, status FROM story_arcs")).all(),
        )
    # The most recently updated arc — the one the read path was already
    # returning — survives; the loser becomes history, not garbage.
    assert statuses["winner"] == "active"
    assert statuses["loser"] == "completed"
    assert statuses["other-active"] == "active"
    assert statuses["old"] == "completed"
    assert statuses["dropped"] == "abandoned"


def test_upgrade_installs_a_partial_index_that_rejects_duplicates(
    engine, sqlite_url,
) -> None:  # noqa: ANN001
    config = _alembic_config(sqlite_url)
    command.stamp(config, _PARENT_REVISION)
    command.upgrade(config, _ARC_REVISION)

    with engine.connect() as conn:
        indexes = {
            row[1]: row
            for row in conn.execute(text("PRAGMA index_list(story_arcs)"))
        }
        assert _INDEX in indexes
        assert indexes[_INDEX][2] == 1  # unique flag
        assert indexes[_INDEX][4] == 1  # partial flag

    with engine.begin() as conn:
        _insert_arc(
            conn, arc_id="first", character_id="c1",
            status="active", updated_at="2026-04-10 10:00:00",
        )
    with pytest.raises(Exception, match="UNIQUE constraint failed"):
        with engine.begin() as conn:
            _insert_arc(
                conn, arc_id="second", character_id="c1",
                status="active", updated_at="2026-04-10 11:00:00",
            )
    # Terminal arcs stay unconstrained — history is unlimited.
    with engine.begin() as conn:
        for index in range(3):
            _insert_arc(
                conn, arc_id=f"done-{index}", character_id="c1",
                status="completed", updated_at="2026-04-10 11:00:00",
            )


def test_downgrade_drops_the_index(engine, sqlite_url) -> None:  # noqa: ANN001
    config = _alembic_config(sqlite_url)
    command.stamp(config, _PARENT_REVISION)
    command.upgrade(config, _ARC_REVISION)
    command.downgrade(config, _PARENT_REVISION)

    with engine.connect() as conn:
        indexes = {
            row[1] for row in conn.execute(text("PRAGMA index_list(story_arcs)"))
        }
    assert _INDEX not in indexes


def test_consolidation_claim_column_is_added_and_dropped(
    engine, sqlite_url,
) -> None:  # noqa: ANN001
    config = _alembic_config(sqlite_url)
    command.stamp(config, _ARC_REVISION)
    command.upgrade(config, _CONSOLIDATION_REVISION)

    with engine.connect() as conn:
        columns = {
            row[1]: row
            for row in conn.execute(text("PRAGMA table_info(characters)"))
        }
    assert "last_consolidated_at" in columns
    # Nullable: NULL reads as "never consolidated", the right start for every
    # pre-existing character (the old state lived only in process memory).
    assert columns["last_consolidated_at"][3] == 0

    command.downgrade(config, _ARC_REVISION)
    with engine.connect() as conn:
        columns = {
            row[1] for row in conn.execute(text("PRAGMA table_info(characters)"))
        }
    assert "last_consolidated_at" not in columns
