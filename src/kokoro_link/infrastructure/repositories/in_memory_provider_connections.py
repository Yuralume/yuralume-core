"""In-memory BYOK provider connection repository."""

from __future__ import annotations

from datetime import datetime, timezone

from kokoro_link.contracts.provider_settings import (
    ProviderCapability,
    ProviderConnection,
    ProviderConnectionRepositoryPort,
)


class InMemoryProviderConnectionRepository(ProviderConnectionRepositoryPort):
    def __init__(
        self,
        seed: list[ProviderConnection] | None = None,
    ) -> None:
        self._rows: dict[str, ProviderConnection] = {
            row.id: row for row in seed or []
        }

    async def list_all(self) -> list[ProviderConnection]:
        return list(self._rows.values())

    async def list_enabled(
        self,
        *,
        capability: ProviderCapability | None = None,
    ) -> list[ProviderConnection]:
        rows = [row for row in self._rows.values() if row.enabled]
        if capability is not None:
            rows = [row for row in rows if capability in row.capabilities]
        return rows

    async def get(self, connection_id: str) -> ProviderConnection | None:
        return self._rows.get(connection_id)

    async def save(self, connection: ProviderConnection) -> ProviderConnection:
        now = datetime.now(timezone.utc)
        existing = self._rows.get(connection.id)
        saved = connection.with_timestamps(
            created_at=connection.created_at or (existing.created_at if existing else now),
            updated_at=connection.updated_at or now,
        )
        self._rows[connection.id] = saved
        return saved

    async def delete(self, connection_id: str) -> None:
        self._rows.pop(connection_id, None)
