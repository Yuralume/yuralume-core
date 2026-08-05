"""Real clean-SQLite upgrade for ``a3r0l7o10029`` (SC0 failure counters).

The backfill is the interesting part, so this runs the real DDL against a
fresh on-disk SQLite database (same shape as
``test_story_arcs_single_active_migration``) with the four row flavours
production actually contains: a beat whose last attempt failed, one that
was merely surfaced to a chatting player, one that produced a scene, and
one never attempted at all.

What the assertions pin is the *semantic* choice, not the SQL: the
migration credits **at most one** failure per row — the only failure the
old schema can prove — instead of copying the inflated
``play_attempt_count`` across, which would let the new retirement path
permanently skip live beats on the first tick after deploy.

The full base→head chain is not runnable on SQLite (an earlier migration
issues ``CREATE EXTENSION vector``), so the prerequisite table is seeded
by hand and the parent revision is stamped.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text

_REPO_ROOT = Path(__file__).parents[2].resolve()
_PARENT_REVISION = "z2q9k6n10028"
_REVISION = "a3r0l7o10029"


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
    attempts: int,
    last_at: str | None,
    last_result: str | None,
) -> None:
    conn.execute(
        text(
            "INSERT INTO story_arc_beats (id, arc_id, sequence, scheduled_date,"
            " title, summary, tension, status, play_attempt_count,"
            " last_play_attempt_at, last_play_attempt_source,"
            " last_play_attempt_result, last_play_push_intensity)"
            " VALUES (:id, 'arc-1', 0, '2026-05-01', 't', 's', 'setup',"
            " 'pending', :attempts, :last_at, 'scene_simulation',"
            " :last_result, 'autonomous_scene')"
        ),
        {
            "id": beat_id,
            "attempts": attempts,
            "last_at": last_at,
            "last_result": last_result,
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


def test_upgrade_credits_at_most_one_provable_failure(
    engine, sqlite_url,
) -> None:  # noqa: ANN001
    with engine.begin() as conn:
        # Scene writer failed on the last try, after a lot of chat noise.
        _insert_beat(
            conn, beat_id="failing", attempts=9,
            last_at="2026-05-01 08:00:00", last_result="failed",
        )
        _insert_beat(
            conn, beat_id="crashed", attempts=2,
            last_at="2026-05-01 09:00:00", last_result="writer_crashed",
        )
        _insert_beat(
            conn, beat_id="empty", attempts=2,
            last_at="2026-05-01 10:00:00", last_result="empty_scene",
        )
        # Surfaced to a chatting player — never failed.
        _insert_beat(
            conn, beat_id="prompted", attempts=12,
            last_at="2026-05-01 11:00:00", last_result="prompted",
        )
        # An autonomous scene was actually written.
        _insert_beat(
            conn, beat_id="written", attempts=1,
            last_at="2026-05-01 12:00:00", last_result="scene_written:solo",
        )
        # Never staged at all.
        _insert_beat(
            conn, beat_id="fresh", attempts=0,
            last_at=None, last_result=None,
        )

    config = _alembic_config(sqlite_url)
    command.stamp(config, _PARENT_REVISION)
    command.upgrade(config, _REVISION)

    with engine.connect() as conn:
        rows = {
            row[0]: (row[1], row[2])
            for row in conn.execute(
                text(
                    "SELECT id, play_failure_count, last_play_failure_at"
                    " FROM story_arc_beats"
                ),
            )
        }

    # One provable failure — NOT the 9 surfacings the old counter held.
    assert rows["failing"][0] == 1
    assert str(rows["failing"][1]).startswith("2026-05-01 08:00:00")
    assert rows["crashed"][0] == 1
    assert rows["empty"][0] == 1
    # Non-failure results keep a full, unspent budget and no anchor.
    assert rows["prompted"] == (0, None)
    assert rows["written"] == (0, None)
    assert rows["fresh"] == (0, None)


def test_downgrade_drops_both_columns(engine, sqlite_url) -> None:  # noqa: ANN001
    config = _alembic_config(sqlite_url)
    command.stamp(config, _PARENT_REVISION)
    command.upgrade(config, _REVISION)
    command.downgrade(config, _PARENT_REVISION)

    with engine.connect() as conn:
        columns = {
            row[1] for row in conn.execute(text("PRAGMA table_info(story_arc_beats)"))
        }
    assert "play_failure_count" not in columns
    assert "last_play_failure_at" not in columns
