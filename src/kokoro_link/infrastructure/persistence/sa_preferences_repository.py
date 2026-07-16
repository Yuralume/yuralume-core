"""SQLAlchemy-backed ``PreferencesRepositoryPort`` implementation.

Uses the ``app_preferences`` KV table — a single row per preference
key, with a JSON-encoded value. Values go through ``json.dumps`` /
``json.loads`` so callers hand in primitives, not strings-of-JSON.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker

from kokoro_link.contracts.repositories import PreferencesRepositoryPort
from kokoro_link.infrastructure.persistence.models import AppPreferenceRow


class SAPreferencesRepository(PreferencesRepositoryPort):
    def __init__(self, session_factory: sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get(self, key: str) -> object | None:
        async with self._session_factory() as session:
            row = await session.get(AppPreferenceRow, key)
            if row is None:
                return None
            try:
                return json.loads(row.value)
            except json.JSONDecodeError:
                # Corrupt row — treat as missing rather than crash the
                # caller. Next ``set`` will overwrite it.
                return None

    async def set(self, key: str, value: object) -> None:
        payload = json.dumps(value, ensure_ascii=False)
        now = datetime.now(timezone.utc)
        async with self._session_factory() as session:
            existing = await session.get(AppPreferenceRow, key)
            if existing is None:
                session.add(AppPreferenceRow(
                    key=key, value=payload, updated_at=now,
                ))
            else:
                existing.value = payload
                existing.updated_at = now
            await session.commit()

    async def delete(self, key: str) -> bool:
        async with self._session_factory() as session:
            existing = await session.get(AppPreferenceRow, key)
            if existing is None:
                return False
            await session.execute(
                delete(AppPreferenceRow).where(AppPreferenceRow.key == key),
            )
            await session.commit()
            return True
