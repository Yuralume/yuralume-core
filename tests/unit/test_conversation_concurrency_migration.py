"""Real clean-SQLite upgrade for the conversation-concurrency migrations.

R-B1a: ``p1n8o3q4017`` originally added its two uniqueness guards with
``op.create_unique_constraint`` (an ``ALTER TABLE ... ADD CONSTRAINT``), which
SQLite cannot run without batch mode — a clean SQLite ``upgrade`` blew up. The
fix builds them as UNIQUE INDEXES. This test runs the *real* migration DDL
against a fresh on-disk SQLite database (not a recording-op stub) so the SQLite
incompatibility can never regress.

R-M8: ``q2o9p4r5018`` backfills pre-existing ``accepted`` ledger rows to
``conversation_recorded=TRUE`` so the first post-upgrade sweep does not
re-append their history. This test upgrades to the pre-M8 revision, inserts an
``accepted`` and a ``pending`` row, then upgrades head and asserts only the
pending row is left for the sweep.

The full base→head chain is not runnable on SQLite (an earlier migration issues
``CREATE EXTENSION vector``), so we seed the minimal prerequisite tables and
stamp ``n9l6m1o2015`` — the revision right before the additive external-proactive
+ conversation-guard migrations under test.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

_REPO_ROOT = Path(__file__).parents[2]
_PRE_LEDGER_REVISION = "n9l6m1o2015"
_PRE_M8_REVISION = "p1n8o3q4017"
# Last revision these tests actually exercise. Pinned instead of ``head``:
# only the prerequisites of the migrations under test are seeded below, so a
# later revision touching another table would fail here for reasons that have
# nothing to do with conversation concurrency.
_M8_REVISION = "q2o9p4r5018"


def _alembic_config(db_url: str) -> Config:
    # Deliberately built WITHOUT the alembic.ini path: env.py runs
    # ``fileConfig(config_file_name)`` when one is set, which reconfigures the
    # logging system with ``disable_existing_loggers=True`` and would silently
    # break every caplog-based test that runs later in the same process. With no
    # config file, env.py skips fileConfig and only the two options below drive
    # the migration.
    config = Config()
    config.set_main_option("script_location", str(_REPO_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", db_url)
    return config


def _seed_prerequisite_tables(engine) -> None:  # noqa: ANN001
    # The pre-``p1n8o3q4017`` shape of the only tables the migrations under test
    # touch (conversations/messages exist well before the stamped revision).
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE conversations ("
                "id VARCHAR PRIMARY KEY, character_id VARCHAR, source VARCHAR)",
            ),
        )
        conn.execute(
            text(
                "CREATE TABLE messages ("
                "id INTEGER PRIMARY KEY, conversation_id VARCHAR, "
                "position INTEGER)",
            ),
        )


@pytest.fixture()
def sqlite_url(tmp_path) -> str:  # noqa: ANN001
    return f"sqlite:///{tmp_path / 'migration.db'}"


def test_clean_sqlite_upgrade_to_head_creates_unique_indexes(
    sqlite_url, monkeypatch,
) -> None:  # noqa: ANN001
    # env.py resolves DATABASE_URL (and load_dotenv would otherwise re-inject the
    # dev Postgres URL from .env); pin it to the SQLite target so the real DDL
    # runs on SQLite.
    monkeypatch.setenv("DATABASE_URL", sqlite_url)
    monkeypatch.delenv("KOKORO_DATABASE_URL", raising=False)
    engine = create_engine(sqlite_url)
    _seed_prerequisite_tables(engine)

    config = _alembic_config(sqlite_url)
    command.stamp(config, _PRE_LEDGER_REVISION)
    # R-B1a: this is the call that used to raise on SQLite.
    command.upgrade(config, _M8_REVISION)

    with engine.connect() as conn:
        index_names = {
            row[0]
            for row in conn.execute(
                text(
                    "SELECT name FROM sqlite_master WHERE type='index' "
                    "AND tbl_name='messages'",
                ),
            )
        }
        assert "uq_messages_conversation_position" in index_names
        assert "uq_messages_conversation_idempotency" in index_names
        # The additive columns landed.
        conv_cols = {
            row[1]
            for row in conn.execute(text("PRAGMA table_info(conversations)"))
        }
        assert "version" in conv_cols
        msg_cols = {
            row[1] for row in conn.execute(text("PRAGMA table_info(messages)"))
        }
        assert "idempotency_key" in msg_cols
        event_cols = {
            row[1]
            for row in conn.execute(
                text("PRAGMA table_info(external_proactive_events)"),
            )
        }
        assert "conversation_recorded" in event_cols
    engine.dispose()


def test_m8_backfill_marks_existing_accepted_rows_recorded(
    sqlite_url, monkeypatch,
) -> None:  # noqa: ANN001
    monkeypatch.setenv("DATABASE_URL", sqlite_url)
    monkeypatch.delenv("KOKORO_DATABASE_URL", raising=False)
    engine = create_engine(sqlite_url)
    _seed_prerequisite_tables(engine)

    config = _alembic_config(sqlite_url)
    command.stamp(config, _PRE_LEDGER_REVISION)
    # Upgrade only up to the revision BEFORE conversation_recorded exists.
    command.upgrade(config, _PRE_M8_REVISION)

    now = "2026-07-24T00:00:00+00:00"
    with engine.begin() as conn:
        for event_id, state in (("accepted-evt", "accepted"), ("pending-evt", "pending")):
            conn.execute(
                text(
                    "INSERT INTO external_proactive_events ("
                    "event_id, tenant_id, account_id, character_id, kind, "
                    "envelope_json, payload_hash, state, accept_attempts, "
                    "created_at, updated_at, expires_at) VALUES ("
                    ":id, 't', 'a', 'c', 'proactive', '{}', 'h', :state, 0, "
                    ":now, :now, :now)",
                ),
                {"id": event_id, "state": state, "now": now},
            )

    # R-M8: the backfill runs inside this upgrade step.
    command.upgrade(config, _M8_REVISION)

    with engine.connect() as conn:
        rows = dict(
            conn.execute(
                text(
                    "SELECT event_id, conversation_recorded "
                    "FROM external_proactive_events",
                ),
            ).all(),
        )
    # Pre-existing accepted row treated as already recorded; pending untouched.
    assert rows["accepted-evt"] == 1
    assert rows["pending-evt"] == 0
    engine.dispose()
