"""SQLAlchemy repository for BYOK provider connections."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker

from kokoro_link.contracts.provider_settings import (
    ProviderCapability,
    ProviderConnection,
    ProviderConnectionRepositoryPort,
)
from kokoro_link.infrastructure.persistence.models import ProviderConnectionRow


class SAProviderConnectionRepository(ProviderConnectionRepositoryPort):
    def __init__(self, session_factory: sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def list_all(self) -> list[ProviderConnection]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(ProviderConnectionRow)
                .where(ProviderConnectionRow.deleted_at.is_(None))
                .order_by(ProviderConnectionRow.created_at.asc()),
            )
            return [_to_domain(row) for row in result.scalars().all()]

    async def list_enabled(
        self,
        *,
        capability: ProviderCapability | None = None,
    ) -> list[ProviderConnection]:
        rows = await self.list_all()
        rows = [row for row in rows if row.enabled]
        if capability is not None:
            rows = [row for row in rows if capability in row.capabilities]
        return rows

    async def get(self, connection_id: str) -> ProviderConnection | None:
        async with self._session_factory() as session:
            row = await session.get(ProviderConnectionRow, connection_id)
            if row is None or row.deleted_at is not None:
                return None
            return _to_domain(row)

    async def save(self, connection: ProviderConnection) -> ProviderConnection:
        async with self._session_factory() as session:
            row = await session.get(ProviderConnectionRow, connection.id)
            now = datetime.now(timezone.utc)
            if row is None:
                row = ProviderConnectionRow(
                    id=connection.id,
                    created_at=connection.created_at or now,
                )
                session.add(row)
            row.provider = connection.provider
            row.label = connection.label
            row.enabled = connection.enabled
            row.capabilities_json = json.dumps(list(connection.capabilities))
            row.config_json = json.dumps(connection.config, ensure_ascii=False)
            row.encrypted_secret_json = connection.encrypted_secret
            row.secret_fingerprint = connection.secret_fingerprint
            row.last_validated_at = connection.last_validated_at
            row.last_validation_error = connection.last_validation_error
            row.updated_at = connection.updated_at or now
            row.deleted_at = None
            await session.commit()
            await session.refresh(row)
            return _to_domain(row)

    async def delete(self, connection_id: str) -> None:
        async with self._session_factory() as session:
            row = await session.get(ProviderConnectionRow, connection_id)
            if row is None:
                return
            row.deleted_at = datetime.now(timezone.utc)
            row.enabled = False
            row.updated_at = row.deleted_at
            await session.commit()


def _to_domain(row: ProviderConnectionRow) -> ProviderConnection:
    return ProviderConnection(
        id=row.id,
        provider=row.provider,
        label=row.label,
        enabled=bool(row.enabled),
        capabilities=tuple(_json_list(row.capabilities_json)),
        config=_json_dict(row.config_json),
        encrypted_secret=row.encrypted_secret_json or "",
        secret_fingerprint=row.secret_fingerprint or "",
        last_validated_at=row.last_validated_at,
        last_validation_error=row.last_validation_error,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _json_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    return [str(item) for item in data if isinstance(item, str)]


def _json_dict(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    return data
