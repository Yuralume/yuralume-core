"""SQLAlchemy repository for account runtime usage events."""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker

from kokoro_link.contracts.account_runtime_usage import AccountRuntimeUsageEvent
from kokoro_link.contracts.clock import ensure_utc
from kokoro_link.infrastructure.persistence.models import AccountRuntimeEventRow


class SAAccountRuntimeUsageRepository:
    def __init__(self, session_factory: sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def record_event(
        self,
        *,
        operator_id: str,
        event_type: str,
        occurred_at: datetime,
        resource_id: str | None = None,
    ) -> None:
        async with self._session_factory() as session:
            session.add(
                AccountRuntimeEventRow(
                    id=str(uuid4()),
                    operator_id=operator_id,
                    event_type=event_type,
                    occurred_at=ensure_utc(occurred_at),
                    resource_id=_normalise_resource_id(resource_id),
                ),
            )
            await session.commit()

    async def count_events(
        self,
        *,
        operator_id: str,
        event_type: str,
        since: datetime,
        until: datetime | None = None,
        resource_id: str | None = None,
    ) -> int:
        since_utc = ensure_utc(since)
        resource = _normalise_resource_id(resource_id)
        stmt = select(func.count()).select_from(AccountRuntimeEventRow).where(
            AccountRuntimeEventRow.operator_id == operator_id,
            AccountRuntimeEventRow.event_type == event_type,
            AccountRuntimeEventRow.occurred_at >= since_utc,
        )
        if until is not None:
            stmt = stmt.where(AccountRuntimeEventRow.occurred_at <= ensure_utc(until))
        if resource is not None:
            stmt = stmt.where(AccountRuntimeEventRow.resource_id == resource)
        async with self._session_factory() as session:
            result = await session.execute(stmt)
            return int(result.scalar_one())

    async def claim_event_slot(
        self,
        *,
        operator_id: str,
        event_type: str,
        occurred_at: datetime,
        since: datetime,
        limit: int,
        resource_id: str | None = None,
        resource_limit: int | None = None,
    ) -> str | None:
        """Write the claim first, then check whether the window still fits.

        A read-then-write ceiling is not a ceiling: two concurrent ticks of the
        same operator (several characters, several hosted replicas) both read
        the same last free slot and both spend it. Inserting first makes every
        racer visible to every other racer's count, so the window can never end
        up over ``limit``. The cost is that a tie may deny both racers when one
        slot was free — the deliberate direction for a service whose whole
        contract is fail-closed: a lost slot is an inconvenience, an unbounded
        purchase loop is the player's money.
        """
        if limit <= 0:
            return None
        event_id = str(uuid4())
        stamp = ensure_utc(occurred_at)
        since_utc = ensure_utc(since)
        resource = _normalise_resource_id(resource_id)
        async with self._session_factory() as session:
            session.add(
                AccountRuntimeEventRow(
                    id=event_id,
                    operator_id=operator_id,
                    event_type=event_type,
                    occurred_at=stamp,
                    resource_id=resource,
                ),
            )
            await session.commit()
            used = await session.execute(
                select(func.count())
                .select_from(AccountRuntimeEventRow)
                .where(
                    AccountRuntimeEventRow.operator_id == operator_id,
                    AccountRuntimeEventRow.event_type == event_type,
                    AccountRuntimeEventRow.occurred_at >= since_utc,
                ),
            )
            if int(used.scalar_one()) > limit:
                await session.execute(
                    delete(AccountRuntimeEventRow).where(
                        AccountRuntimeEventRow.id == event_id,
                    ),
                )
                await session.commit()
                return None
            if resource is not None and resource_limit is not None:
                resource_used = await session.execute(
                    select(func.count())
                    .select_from(AccountRuntimeEventRow)
                    .where(
                        AccountRuntimeEventRow.operator_id == operator_id,
                        AccountRuntimeEventRow.event_type == event_type,
                        AccountRuntimeEventRow.occurred_at >= since_utc,
                        AccountRuntimeEventRow.resource_id == resource,
                    ),
                )
                if int(resource_used.scalar_one()) > resource_limit:
                    await session.execute(
                        delete(AccountRuntimeEventRow).where(
                            AccountRuntimeEventRow.id == event_id,
                        ),
                    )
                    await session.commit()
                    return None
        return event_id

    async def discard_event(self, *, event_id: str) -> None:
        async with self._session_factory() as session:
            await session.execute(
                delete(AccountRuntimeEventRow).where(
                    AccountRuntimeEventRow.id == event_id,
                ),
            )
            await session.commit()

    async def list_events(
        self,
        *,
        event_type: str,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[AccountRuntimeUsageEvent]:
        stmt = select(AccountRuntimeEventRow).where(
            AccountRuntimeEventRow.event_type == event_type,
        )
        if since is not None:
            stmt = stmt.where(AccountRuntimeEventRow.occurred_at >= ensure_utc(since))
        if until is not None:
            stmt = stmt.where(AccountRuntimeEventRow.occurred_at <= ensure_utc(until))
        stmt = stmt.order_by(AccountRuntimeEventRow.occurred_at.asc())
        async with self._session_factory() as session:
            result = await session.execute(stmt)
            return [
                AccountRuntimeUsageEvent(
                    operator_id=row.operator_id,
                    event_type=row.event_type,
                    occurred_at=ensure_utc(row.occurred_at),
                    resource_id=row.resource_id,
                )
                for row in result.scalars()
            ]


def _normalise_resource_id(resource_id: str | None) -> str | None:
    if resource_id is None:
        return None
    value = resource_id.strip()
    return value or None
