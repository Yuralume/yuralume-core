"""Real clean-SQLite upgrade for ``r3p0q5s6019``.

The album gacha's pending batch moved from a per-process dict into
``character_image_candidate_batches`` so that a commit served by a different
api replica can still find it. This runs the *real* migration DDL against a
fresh on-disk SQLite database (not a recording-op stub), the same way
``test_conversation_concurrency_migration`` does, so a dialect-incompatible
DDL edit cannot regress.

The full base→head chain is not runnable on SQLite (an earlier migration issues
``CREATE EXTENSION vector``), so we seed the one prerequisite table the new
foreign key points at and stamp the parent revision.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

_REPO_ROOT = Path(__file__).parents[2]
_PARENT_REVISION = "q2o9p4r5018"
_REVISION = "r3p0q5s6019"
_TABLE = "character_image_candidate_batches"


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
        conn.execute(text("CREATE TABLE characters (id VARCHAR PRIMARY KEY)"))


@pytest.fixture()
def sqlite_url(tmp_path) -> str:  # noqa: ANN001
    return f"sqlite:///{tmp_path / 'migration.db'}"


def test_upgrade_creates_the_candidate_batch_table(
    sqlite_url, monkeypatch,
) -> None:  # noqa: ANN001
    # env.py resolves DATABASE_URL (and load_dotenv would otherwise re-inject
    # the dev Postgres URL from .env); pin it to the SQLite target.
    monkeypatch.setenv("DATABASE_URL", sqlite_url)
    monkeypatch.delenv("KOKORO_DATABASE_URL", raising=False)
    engine = create_engine(sqlite_url)
    _seed_prerequisite_tables(engine)

    config = _alembic_config(sqlite_url)
    command.stamp(config, _PARENT_REVISION)
    # Pinned to this revision, not ``head``: only the prerequisites of THIS
    # migration are seeded above, so a later revision touching another table
    # would fail here for reasons that have nothing to do with what is tested.
    command.upgrade(config, _REVISION)

    with engine.connect() as conn:
        columns = {
            row[1]: row for row in conn.execute(text(f"PRAGMA table_info({_TABLE})"))
        }
        assert set(columns) == {"character_id", "object_keys_json", "created_at"}
        # One active batch per character — the primary key is what enforces
        # "a second generate supersedes the first".
        assert columns["character_id"][5] == 1  # pk flag
        assert columns["object_keys_json"][3] == 1  # NOT NULL
        assert columns["created_at"][3] == 1
        foreign_keys = list(
            conn.execute(text(f"PRAGMA foreign_key_list({_TABLE})")),
        )
        assert [(row[2], row[3]) for row in foreign_keys] == [
            ("characters", "character_id"),
        ]
    engine.dispose()


def test_downgrade_drops_the_candidate_batch_table(
    sqlite_url, monkeypatch,
) -> None:  # noqa: ANN001
    monkeypatch.setenv("DATABASE_URL", sqlite_url)
    monkeypatch.delenv("KOKORO_DATABASE_URL", raising=False)
    engine = create_engine(sqlite_url)
    _seed_prerequisite_tables(engine)

    config = _alembic_config(sqlite_url)
    command.stamp(config, _PARENT_REVISION)
    command.upgrade(config, _REVISION)
    command.downgrade(config, _PARENT_REVISION)

    with engine.connect() as conn:
        tables = {
            row[0]
            for row in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'"),
            )
        }
    assert _TABLE not in tables
    engine.dispose()
