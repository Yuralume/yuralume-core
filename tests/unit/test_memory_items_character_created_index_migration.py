"""Real clean-SQLite upgrade for ``e7v4p8s10033`` (IV8 browse index).

Same shape as ``test_story_scene_operator_position_migration``: seed the
prerequisite table by hand, stamp the parent, run the real DDL. The full
base->head chain is not runnable on SQLite (an earlier migration issues
``CREATE EXTENSION vector``), and ``memory_items`` itself carries two
``vector(1024)`` columns — so the seeded table uses plain columns for
them. The index under test does not touch either one, which is rather
the point of the ticket.

What matters here is that the index covers **both** columns in the order
the browse query filters and sorts by. A ``created_at``-only index would
still leave Postgres filtering the whole table per character; a
``character_id``-only index is what already existed and is exactly what
was too slow.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text

_REPO_ROOT = Path(__file__).parents[2].resolve()
_PARENT_REVISION = "d6u3o0r10032"
_REVISION = "e7v4p8s10033"
_INDEX = "ix_memory_items_character_created"


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
                "CREATE TABLE memory_items ("
                "  id VARCHAR PRIMARY KEY,"
                "  character_id VARCHAR NOT NULL,"
                "  conversation_id VARCHAR,"
                "  kind VARCHAR NOT NULL,"
                "  content TEXT NOT NULL,"
                "  salience FLOAT NOT NULL,"
                "  tags TEXT NOT NULL DEFAULT '[]',"
                "  created_at TIMESTAMP NOT NULL,"
                "  last_accessed_at TIMESTAMP,"
                "  access_count INTEGER NOT NULL DEFAULT 0,"
                "  embedding BLOB,"
                "  tags_embedding BLOB,"
                "  participants_json TEXT NOT NULL DEFAULT '[]',"
                "  world_id VARCHAR,"
                "  location VARCHAR,"
                "  audience VARCHAR NOT NULL DEFAULT ''"
                ")"
            ),
        )


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


def _index_names(conn) -> set[str]:  # noqa: ANN001
    return {
        row[1]
        for row in conn.execute(text("PRAGMA index_list(memory_items)")).all()
    }


def test_revision_chains_to_prior_head_and_graph_stays_linear() -> None:
    config = Config(str(_REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_REPO_ROOT / "alembic"))
    script = ScriptDirectory.from_config(config)

    # Not ``heads == [_REVISION]`` on purpose: a sibling ticket landing a
    # migration on top of this one must not turn this assertion red.
    heads = list(script.get_heads())
    assert len(heads) == 1
    ancestry = {r.revision for r in script.iterate_revisions(heads[0], "base")}
    assert _REVISION in ancestry
    revision = script.get_revision(_REVISION)
    assert revision.down_revision == _PARENT_REVISION


def test_upgrade_creates_the_composite_index(engine, sqlite_url) -> None:  # noqa: ANN001
    with engine.begin() as conn:
        assert _INDEX not in _index_names(conn)

    _upgrade(sqlite_url)

    with engine.begin() as conn:
        assert _INDEX in _index_names(conn)
        columns = [
            row[2]
            for row in conn.execute(
                text(f"PRAGMA index_info({_INDEX})"),
            ).all()
        ]

    # Filter column first, ordering column second — the composite only
    # earns its keep in that order.
    assert columns == ["character_id", "created_at"]


def test_upgrade_touches_no_row_data(engine, sqlite_url) -> None:  # noqa: ANN001
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO memory_items (id, character_id, kind, content,"
                " salience, created_at)"
                " VALUES ('m-1', 'char-1', 'semantic', '記得下雨那天', 0.5,"
                " '2026-06-01 12:00:00')"
            ),
        )

    _upgrade(sqlite_url)

    with engine.begin() as conn:
        rows = conn.execute(
            text("SELECT id, character_id, content FROM memory_items"),
        ).all()

    assert [tuple(row) for row in rows] == [("m-1", "char-1", "記得下雨那天")]


def test_downgrade_drops_only_the_index(engine, sqlite_url) -> None:  # noqa: ANN001
    _upgrade(sqlite_url)
    config = _alembic_config(sqlite_url)
    command.downgrade(config, _PARENT_REVISION)

    with engine.begin() as conn:
        assert _INDEX not in _index_names(conn)
        columns = {
            row[1]
            for row in conn.execute(
                text("PRAGMA table_info(memory_items)"),
            ).all()
        }

    assert {"character_id", "created_at", "embedding", "tags_embedding"} <= columns
