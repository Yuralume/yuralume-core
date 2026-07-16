"""SQLAlchemy async engine + session factory wiring.

Targets PostgreSQL via asyncpg. Pool settings are tuned for the typical
per-turn burst (~3-5 concurrent writes: main request + post-turn +
memorialize + schedule adjustments).
"""

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker


def build_async_engine(database_url: str) -> AsyncEngine:
    """Create an async SQLAlchemy engine from a ``postgresql+asyncpg://`` URL."""
    kwargs: dict = {"echo": False, "future": True}
    if database_url.startswith("postgresql+asyncpg"):
        kwargs.update(
            pool_size=5,
            max_overflow=10,
            pool_pre_ping=True,
            pool_recycle=1800,
        )
    return create_async_engine(database_url, **kwargs)


def build_session_factory(engine: AsyncEngine) -> sessionmaker[AsyncSession]:
    return sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
