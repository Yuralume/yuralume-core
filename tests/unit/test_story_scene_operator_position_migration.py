"""Real clean-SQLite upgrade for ``d6u3o0r10032`` (OP2-D player position on
the scene session).

Same shape as ``test_story_arc_beat_operator_position_migration``: seed the
prerequisite table by hand (the full ``story_scene_sessions`` column set as
of ``c5t2n9q10031``, unchanged since ``b4s1m8p10030`` created it), stamp
the parent, run the real DDL. The full base->head chain is not runnable on
SQLite (an earlier migration issues ``CREATE EXTENSION vector``).

The assertion that matters is the *absence* of a decision: every scene
session that existed before this column has no position to backfill from
— the column did not exist when those sessions were opened — so ``NULL``
(unjudged) is the only honest value for stock rows (§4 #2 存量零洗資料).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text

_REPO_ROOT = Path(__file__).parents[2].resolve()
_PARENT_REVISION = "c5t2n9q10031"
_REVISION = "d6u3o0r10032"


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
                "CREATE TABLE story_scene_sessions ("
                "  id VARCHAR PRIMARY KEY,"
                "  character_id VARCHAR NOT NULL,"
                "  conversation_id VARCHAR NOT NULL,"
                "  status VARCHAR NOT NULL,"
                "  source_layer VARCHAR NOT NULL,"
                "  arc_id VARCHAR,"
                "  beat_id VARCHAR,"
                "  title TEXT NOT NULL DEFAULT '',"
                "  location TEXT,"
                "  mood VARCHAR,"
                "  scene_type VARCHAR,"
                "  dramatic_question TEXT,"
                "  opened_at TIMESTAMP NOT NULL,"
                "  last_activity_at TIMESTAMP NOT NULL,"
                "  closed_at TIMESTAMP,"
                "  closed_reason VARCHAR"
                ")"
            ),
        )


def _insert_session(conn, *, session_id: str) -> None:  # noqa: ANN001
    conn.execute(
        text(
            "INSERT INTO story_scene_sessions (id, character_id,"
            " conversation_id, status, source_layer, title, opened_at,"
            " last_activity_at)"
            " VALUES (:id, 'char-1', 'conv-1', 'open', 'beat', 't',"
            " '2026-06-01 12:00:00', '2026-06-01 12:00:00')"
        ),
        {"id": session_id},
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


def test_revision_chains_to_prior_head_and_graph_stays_linear() -> None:
    config = Config(str(_REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_REPO_ROOT / "alembic"))
    script = ScriptDirectory.from_config(config)

    # Not ``heads == [_REVISION]`` on purpose (2026-08-01: OP0-A's own
    # migration test tripped on the earlier, stricter version of this
    # assertion once a sibling ticket's migration landed on top of it).
    heads = list(script.get_heads())
    assert len(heads) == 1
    ancestry = {r.revision for r in script.iterate_revisions(heads[0], "base")}
    assert _REVISION in ancestry
    revision = script.get_revision(_REVISION)
    assert revision.down_revision == _PARENT_REVISION


def test_upgrade_leaves_every_existing_session_unjudged(
    engine, sqlite_url,  # noqa: ANN001
) -> None:
    with engine.begin() as conn:
        _insert_session(conn, session_id="scene-plain")

    _upgrade(sqlite_url)

    with engine.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT id, operator_position, operator_note"
                " FROM story_scene_sessions ORDER BY id"
            ),
        ).all()

    assert [tuple(row) for row in rows] == [("scene-plain", None, None)]


def test_upgrade_accepts_the_three_positions_and_a_note(
    engine, sqlite_url,  # noqa: ANN001
) -> None:
    _upgrade(sqlite_url)

    with engine.begin() as conn:
        for index, position in enumerate(("absent", "present", "central")):
            conn.execute(
                text(
                    "INSERT INTO story_scene_sessions (id, character_id,"
                    " conversation_id, status, source_layer, title,"
                    " opened_at, last_activity_at, operator_position,"
                    " operator_note)"
                    " VALUES (:id, 'char-1', 'conv-1', 'closed', 'beat',"
                    " 't', '2026-06-01 12:00:00', '2026-06-01 12:00:00',"
                    " :position, :note)"
                ),
                {
                    "id": f"scene-{position}",
                    "position": position,
                    "note": "她要向你坦白",
                },
            )

    with engine.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT operator_position, operator_note"
                " FROM story_scene_sessions ORDER BY id"
            ),
        ).all()

    assert {row[0] for row in rows} == {"absent", "present", "central"}
    assert {row[1] for row in rows} == {"她要向你坦白"}


def test_downgrade_drops_both_columns(engine, sqlite_url) -> None:  # noqa: ANN001
    _upgrade(sqlite_url)
    config = _alembic_config(sqlite_url)
    command.downgrade(config, _PARENT_REVISION)

    with engine.begin() as conn:
        columns = {
            row[1]
            for row in conn.execute(
                text("PRAGMA table_info(story_scene_sessions)"),
            ).all()
        }

    assert "operator_position" not in columns
    assert "operator_note" not in columns
    # The session's own fields are untouched by the round trip.
    assert {"scene_type", "dramatic_question", "closed_reason"} <= columns
