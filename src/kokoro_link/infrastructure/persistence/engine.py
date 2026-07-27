"""SQLAlchemy async engine + session factory wiring.

Targets PostgreSQL via asyncpg. Pool settings are tuned for the typical
per-turn burst (~3-5 concurrent writes: main request + post-turn +
memorialize + schedule adjustments).
"""

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker


def build_async_engine(
    database_url: str,
    *,
    pool_size: int = 5,
    max_overflow: int = 10,
) -> AsyncEngine:
    """Create an async SQLAlchemy engine from a ``postgresql+asyncpg://`` URL.

    ``pool_size`` / ``max_overflow`` are the per-process connection budget
    (YURALUME_DB_POOL_SIZE / YURALUME_DB_MAX_OVERFLOW). They apply only on
    the pooled asyncpg path; the SQLite / other-dialect fallback ignores
    them exactly as before.
    """
    kwargs: dict = {"echo": False, "future": True}
    if database_url.startswith("postgresql+asyncpg"):
        kwargs.update(
            pool_size=pool_size,
            max_overflow=max_overflow,
            pool_pre_ping=True,
            pool_recycle=1800,
        )
    return create_async_engine(database_url, **kwargs)


def build_session_factory(engine: AsyncEngine) -> sessionmaker[AsyncSession]:
    return sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
