"""Shared pytest fixtures.

The project is PostgreSQL-only at runtime. For integration tests that
need a real database, a session-scoped ``testcontainers`` Postgres is
spun up once per test run (via docker). Each test then gets a fresh
schema via drop/create_all — cheap enough for the current size and
much simpler than wiring up savepoints across async sessions.

Unit tests that don't need a DB keep using the in-memory repository
implementations; no fixtures from this module are imported.

If Docker is not running, the ``postgres_container`` fixture raises
``pytest.skip`` so the suite can still execute the (larger) in-memory
test portion offline.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.orm import sessionmaker

from kokoro_link.infrastructure.persistence.engine import build_session_factory
from kokoro_link.infrastructure.persistence.models import Base


_POSTGRES_IMAGE = "pgvector/pgvector:pg16"


@pytest.fixture(scope="session")
def postgres_container() -> Iterator[object]:
    """Session-scoped PostgreSQL container.

    Skipped when Docker is not reachable so offline contributors can
    still run the unit-test subset (the integration suite is the only
    thing that asks for this fixture).
    """
    try:
        from testcontainers.postgres import PostgresContainer
    except ImportError:
        pytest.skip("testcontainers not installed")

    try:
        container = PostgresContainer(
            image=_POSTGRES_IMAGE,
            username="kokoro",
            password="kokoro_test",
            dbname="kokoro_test",
            driver="asyncpg",
        )
        container.start()
    except Exception as exc:  # docker not running / image pull failed / etc.
        pytest.skip(f"Docker not available for testcontainers: {exc}")

    try:
        yield container
    finally:
        try:
            container.stop()
        except Exception:
            pass


def _async_url(container) -> str:
    """Return an ``asyncpg`` SQLAlchemy URL for the running container."""
    # testcontainers' get_connection_url returns a psycopg2-flavoured URL
    # like ``postgresql+psycopg2://...``. Convert to asyncpg for our app.
    url = container.get_connection_url()
    if "+psycopg2" in url:
        url = url.replace("+psycopg2", "+asyncpg")
    elif url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


@pytest_asyncio.fixture
async def engine(postgres_container) -> AsyncIterator[AsyncEngine]:
    """Fresh schema per test.

    ``drop_all`` + ``create_all`` runs quickly (< 100ms) against the
    already-running container and gives every test a pristine state
    without coordinating transactions across fixtures.

    The ``vector`` extension is created defensively — the
    ``pgvector/pgvector`` image ships it but a raw ``CREATE EXTENSION``
    requires superuser, which the testcontainer provides.

    Seeds the default operator row after ``create_all`` so any
    integration test that inserts a character (FK
    ``characters.user_id → operator_profiles.id``) finds an owner
    waiting. Alembic migration ct5y7z00070 does the same on real
    deployments; ``create_all`` skips migrations so we replicate the
    seed here.
    """
    from datetime import datetime, timezone

    from sqlalchemy import text

    url = _async_url(postgres_container)
    eng = create_async_engine(url, echo=False, future=True)
    async with eng.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
        now = datetime.now(timezone.utc)
        await conn.execute(
            text(
                "INSERT INTO operator_profiles "
                "(id, display_name, aliases_json, pronouns, email, "
                "password_hash, is_admin, created_at, updated_at) "
                "VALUES (:id, :name, '[]', NULL, NULL, NULL, TRUE, "
                ":now, :now)"
            ),
            {"id": "default", "name": "操作者", "now": now},
        )
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest_asyncio.fixture
async def session_factory(engine: AsyncEngine) -> sessionmaker:
    return build_session_factory(engine)
