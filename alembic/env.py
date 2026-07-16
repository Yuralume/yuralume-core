import os
from logging.config import fileConfig
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import engine_from_config, pool

from alembic import context
from kokoro_link.infrastructure.persistence.models import Base

# Load .env so `alembic upgrade head` and `make db-migrate` pick up the
# same ``KOKORO_DATABASE_URL`` the app uses at runtime.
load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Allow env var override for database URL
env_url = os.getenv("DATABASE_URL") or os.getenv("KOKORO_DATABASE_URL")
if env_url:
    # Alembic uses synchronous drivers — swap asyncpg → psycopg2 so the
    # same URL can drive both the app and migrations.
    sync_url = env_url.replace("+asyncpg", "+psycopg2")
    config.set_main_option("sqlalchemy.url", sync_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
