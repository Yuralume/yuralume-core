"""Real clean-SQLite upgrade for ``b4s1m8p10030`` (SC1-A scene sessions).

Mirrors ``test_story_arc_beat_play_failures_migration`` in shape: the DDL
is run for real against a fresh on-disk SQLite database rather than
asserted against the ORM metadata, because the two can drift and only the
migration is what a deployment actually executes.

What is pinned is the invariant, not the column list: the partial unique
index must reject a second ``open`` row for one character while leaving
``closed`` history unconstrained. An index accidentally written without
its ``WHERE`` clause would pass a metadata comparison and then stop every
player from ever opening a second scene.

The full base→head chain is not runnable on SQLite (an earlier migration
issues ``CREATE EXTENSION vector``), so the FK targets are seeded by hand
and the parent revision is stamped.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

_REPO_ROOT = Path(__file__).parents[2].resolve()
_PARENT_REVISION = "a3r0l7o10029"
_REVISION = "b4s1m8p10030"


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
            text("CREATE TABLE characters (id VARCHAR PRIMARY KEY)"),
        )
        conn.execute(
            text("CREATE TABLE conversations (id VARCHAR PRIMARY KEY)"),
        )
        conn.execute(text("INSERT INTO characters (id) VALUES ('char-1')"))
        conn.execute(text("INSERT INTO characters (id) VALUES ('char-2')"))
        conn.execute(text("INSERT INTO conversations (id) VALUES ('conv-1')"))


def _insert_scene(
    conn,  # noqa: ANN001
    *,
    scene_id: str,
    character_id: str = "char-1",
    status: str = "open",
) -> None:
    conn.execute(
        text(
            "INSERT INTO story_scene_sessions (id, character_id,"
            " conversation_id, status, source_layer, title, opened_at,"
            " last_activity_at)"
            " VALUES (:id, :character_id, 'conv-1', :status, 'beat', 't',"
            " '2026-06-01 12:00:00', '2026-06-01 12:00:00')"
        ),
        {"id": scene_id, "character_id": character_id, "status": status},
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


def _upgrade(sqlite_url: str) -> None:
    config = _alembic_config(sqlite_url)
    command.stamp(config, _PARENT_REVISION)
    command.upgrade(config, _REVISION)


def test_revision_chains_to_prior_head_and_graph_stays_linear() -> None:
    config = Config(str(_REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_REPO_ROOT / "alembic"))
    script = ScriptDirectory.from_config(config)

    # The graph stays single-headed and this revision stays on it. The
    # assertion is deliberately *not* ``heads == [_REVISION]``: this
    # migration is only the head until the next one chains onto it, and
    # pinning it that way makes an unrelated schema change look like a
    # regression here (2026-08-01: OP0-A's beat player-position column
    # tripped exactly that).
    heads = list(script.get_heads())
    assert len(heads) == 1
    ancestry = {
        entry.revision
        for entry in script.iterate_revisions(heads[0], "base")
    }
    assert _REVISION in ancestry
    revision = script.get_revision(_REVISION)
    assert revision.down_revision == _PARENT_REVISION


def test_one_open_scene_per_character_is_enforced_by_the_schema(
    engine, sqlite_url,
) -> None:  # noqa: ANN001
    _upgrade(sqlite_url)

    with engine.begin() as conn:
        _insert_scene(conn, scene_id="scene-1")

    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            _insert_scene(conn, scene_id="scene-2")

    # A different character is unaffected — the index is per character.
    with engine.begin() as conn:
        _insert_scene(conn, scene_id="scene-3", character_id="char-2")
        rows = conn.execute(
            text("SELECT COUNT(*) FROM story_scene_sessions"),
        ).scalar()
    assert rows == 2


def test_closed_scene_history_is_unconstrained(engine, sqlite_url) -> None:  # noqa: ANN001
    """The index must carry its ``WHERE status = 'open'`` clause: without
    it a character could never play a second scene, ever."""
    _upgrade(sqlite_url)

    with engine.begin() as conn:
        for index in range(4):
            _insert_scene(
                conn, scene_id=f"closed-{index}", status="closed",
            )
        _insert_scene(conn, scene_id="live")

    with engine.connect() as conn:
        assert conn.execute(
            text(
                "SELECT COUNT(*) FROM story_scene_sessions"
                " WHERE status = 'closed'"
            ),
        ).scalar() == 4
        assert conn.execute(
            text(
                "SELECT COUNT(*) FROM story_scene_sessions"
                " WHERE status = 'open'"
            ),
        ).scalar() == 1


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
    assert "story_scene_sessions" not in tables
