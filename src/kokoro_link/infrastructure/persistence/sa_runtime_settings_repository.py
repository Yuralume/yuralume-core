"""SQLAlchemy runtime settings repository (HUMANIZATION_ROADMAP §4.5)."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker

from kokoro_link.contracts.runtime_settings import RuntimeSettingsRepositoryPort
from kokoro_link.infrastructure.persistence.models import AppRuntimeSettingRow


class SARuntimeSettingsRepository(RuntimeSettingsRepositoryPort):
    """Persistent KV backed by ``app_runtime_settings``.

    Uses dialect-specific ``ON CONFLICT`` upsert so a single call writes
    or updates a key without a read-modify-write round trip — matters
    because admin endpoints can touch the same key concurrently.
    """

    def __init__(self, session_factory: sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get(self, key: str) -> str | None:
        async with self._session_factory() as session:
            result = await session.execute(
                select(AppRuntimeSettingRow.value).where(
                    AppRuntimeSettingRow.key == key,
                ),
            )
            row = result.scalar_one_or_none()
            return row if row is not None else None

    async def set(self, key: str, value: str) -> None:
        async with self._session_factory() as session:
            dialect = session.bind.dialect.name if session.bind else ""
            now = datetime.now(timezone.utc)
            if dialect == "postgresql":
                stmt = pg_insert(AppRuntimeSettingRow).values(
                    key=key, value=value, updated_at=now,
                )
                stmt = stmt.on_conflict_do_update(
                    index_elements=[AppRuntimeSettingRow.key],
                    set_={"value": value, "updated_at": now},
                )
            else:
                stmt = sqlite_insert(AppRuntimeSettingRow).values(
                    key=key, value=value, updated_at=now,
                )
                stmt = stmt.on_conflict_do_update(
                    index_elements=[AppRuntimeSettingRow.key],
                    set_={"value": value, "updated_at": now},
                )
            await session.execute(stmt)
            await session.commit()

    async def all(self) -> dict[str, str]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(AppRuntimeSettingRow.key, AppRuntimeSettingRow.value),
            )
            return {row.key: row.value for row in result.all()}
