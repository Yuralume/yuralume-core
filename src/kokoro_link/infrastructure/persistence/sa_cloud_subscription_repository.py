"""SQLAlchemy Cloud tenant subscription state repository."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker

from kokoro_link.contracts.cloud_subscription import (
    CloudSubscriptionRepositoryPort,
)
from kokoro_link.domain.entities.cloud_subscription import CloudSubscriptionState
from kokoro_link.infrastructure.persistence.models import CloudSubscriptionStateRow


class SACloudSubscriptionRepository(CloudSubscriptionRepositoryPort):
    def __init__(self, session_factory: sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get(self, tenant_id: str) -> CloudSubscriptionState | None:
        key = _normalise_tenant_id(tenant_id)
        async with self._session_factory() as session:
            row = await session.get(CloudSubscriptionStateRow, key)
            return _to_domain(row) if row is not None else None

    async def set_locked(
        self,
        tenant_id: str,
        *,
        locked: bool,
        updated_at: datetime | None = None,
    ) -> CloudSubscriptionState:
        key = _normalise_tenant_id(tenant_id)
        when = updated_at or datetime.now(timezone.utc)
        statement = (
            insert(CloudSubscriptionStateRow)
            .values(tenant_id=key, locked=bool(locked), updated_at=when)
            .on_conflict_do_update(
                index_elements=[CloudSubscriptionStateRow.tenant_id],
                set_={"locked": bool(locked), "updated_at": when},
            )
        )
        async with self._session_factory() as session:
            await session.execute(statement)
            await session.commit()
        return CloudSubscriptionState(
            tenant_id=key,
            locked=bool(locked),
            updated_at=when,
        )


def _to_domain(row: CloudSubscriptionStateRow) -> CloudSubscriptionState:
    updated_at = row.updated_at
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)
    return CloudSubscriptionState(
        tenant_id=row.tenant_id,
        locked=bool(row.locked),
        updated_at=updated_at,
    )


def _normalise_tenant_id(tenant_id: str) -> str:
    value = (tenant_id or "").strip()
    if not value:
        raise ValueError("tenant_id must be non-empty")
    return value
