"""Real clean-SQLite upgrade for ``c5t2n9q10031`` (OP0-A player position).

Same shape as ``test_story_arc_beat_play_failures_migration``: seed the
prerequisite table by hand, stamp the parent, run the real DDL. The full
base→head chain is not runnable on SQLite (an earlier migration issues
``CREATE EXTENSION vector``).

The assertion that matters is the *absence* of a decision. ``NULL`` here
is not an unfilled value — it is the domain's fourth state, *unjudged* —
so every beat that existed before this column must come out of the
upgrade still saying "nobody decided". A server default or a backfill
would hand thousands of stock beats a player framing no author chose,
which is the plan's §4 #2 red line (存量零洗資料).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text

_REPO_ROOT = Path(__file__).parents[2].resolve()
_PARENT_REVISION = "b4s1m8p10030"
_REVISION = "c5t2n9q10031"


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
                "CREATE TABLE story_arc_beats ("
                "  id VARCHAR PRIMARY KEY,"
                "  arc_id VARCHAR NOT NULL,"
                "  sequence INTEGER NOT NULL,"
                "  scheduled_date VARCHAR NOT NULL,"
                "  title TEXT NOT NULL,"
                "  summary TEXT NOT NULL,"
                "  tension VARCHAR NOT NULL,"
                "  status VARCHAR NOT NULL,"
                "  realized_event_id VARCHAR,"
                "  play_attempt_count INTEGER NOT NULL DEFAULT 0,"
                "  last_play_attempt_at TIMESTAMP,"
                "  last_play_attempt_source VARCHAR,"
                "  last_play_attempt_result VARCHAR,"
                "  last_play_push_intensity VARCHAR,"
                "  play_failure_count INTEGER NOT NULL DEFAULT 0,"
                "  last_play_failure_at TIMESTAMP,"
                "  scene_characters TEXT NOT NULL DEFAULT '[]',"
                "  location TEXT,"
                "  dramatic_question TEXT,"
                "  scene_type VARCHAR NOT NULL DEFAULT 'encounter',"
                "  required BOOLEAN NOT NULL DEFAULT 1"
                ")"
            ),
        )


def _insert_beat(
    conn,  # noqa: ANN001
    *,
    beat_id: str,
    scene_characters: str = "[]",
) -> None:
    conn.execute(
        text(
            "INSERT INTO story_arc_beats (id, arc_id, sequence,"
            " scheduled_date, title, summary, tension, status,"
            " scene_characters)"
            " VALUES (:id, 'arc-1', 0, '2026-05-01', 't', 's', 'setup',"
            " 'pending', :cast)"
        ),
        {"id": beat_id, "cast": scene_characters},
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

    heads = list(script.get_heads())
    assert len(heads) == 1
    revision = script.get_revision(_REVISION)
    assert revision.down_revision == _PARENT_REVISION
    ancestry = {r.revision for r in script.iterate_revisions(heads[0], "base")}
    assert _REVISION in ancestry


def test_upgrade_leaves_every_existing_beat_unjudged(
    engine, sqlite_url,  # noqa: ANN001
) -> None:
    """Zero data migration. Notably the "伴侶"-style cast — the case the
    plan wants restated as a structured position — is *not* pattern
    matched here: retagging shipped content is authoring work (OP0-B),
    never something a migration guesses from a string."""
    with engine.begin() as conn:
        _insert_beat(conn, beat_id="beat-plain")
        _insert_beat(conn, beat_id="beat-partner", scene_characters='["伴侶"]')

    _upgrade(sqlite_url)

    with engine.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT id, operator_position, operator_note"
                " FROM story_arc_beats ORDER BY id"
            ),
        ).all()

    assert [tuple(row) for row in rows] == [
        ("beat-partner", None, None),
        ("beat-plain", None, None),
    ]


def test_upgrade_accepts_the_three_positions_and_a_note(
    engine, sqlite_url,  # noqa: ANN001
) -> None:
    """The column has to actually hold what the domain can produce —
    including the longest label — on a real engine."""
    _upgrade(sqlite_url)

    with engine.begin() as conn:
        for index, position in enumerate(("absent", "present", "central")):
            conn.execute(
                text(
                    "INSERT INTO story_arc_beats (id, arc_id, sequence,"
                    " scheduled_date, title, summary, tension, status,"
                    " operator_position, operator_note)"
                    " VALUES (:id, 'arc-1', :seq, '2026-05-01', 't', 's',"
                    " 'setup', 'pending', :position, :note)"
                ),
                {
                    "id": f"beat-{position}",
                    "seq": index,
                    "position": position,
                    "note": "她要向你坦白",
                },
            )

    with engine.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT operator_position, operator_note"
                " FROM story_arc_beats ORDER BY sequence"
            ),
        ).all()

    assert [row[0] for row in rows] == ["absent", "present", "central"]
    assert {row[1] for row in rows} == {"她要向你坦白"}


def test_downgrade_drops_both_columns(engine, sqlite_url) -> None:  # noqa: ANN001
    _upgrade(sqlite_url)
    config = _alembic_config(sqlite_url)
    command.downgrade(config, _PARENT_REVISION)

    with engine.begin() as conn:
        columns = {
            row[1]
            for row in conn.execute(
                text("PRAGMA table_info(story_arc_beats)"),
            ).all()
        }

    assert "operator_position" not in columns
    assert "operator_note" not in columns
    # The beat's own fields are untouched by the round trip.
    assert {"scene_characters", "dramatic_question", "scene_type"} <= columns
