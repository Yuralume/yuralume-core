"""SQLAlchemy address-change log repository."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker

from kokoro_link.contracts.address_change_log import AddressChangeLogRepositoryPort
from kokoro_link.domain.value_objects.address_change_event import AddressChangeEvent
from kokoro_link.infrastructure.persistence.models import OperatorAddressChangeLogRow


class SAAddressChangeLogRepository(AddressChangeLogRepositoryPort):
    def __init__(self, session_factory: sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def record(self, event: AddressChangeEvent) -> AddressChangeEvent:
        now = datetime.now(timezone.utc)
        created_at = event.created_at or now
        stamped = replace(
            event,
            id=event.id or uuid.uuid4().hex,
            created_at=created_at,
            effective_at=event.effective_at or created_at,
        )
        async with self._session_factory() as session:
            session.add(
                OperatorAddressChangeLogRow(
                    id=stamped.id,
                    character_id=stamped.character_id,
                    operator_id=stamped.operator_id,
                    direction=stamped.direction,
                    old_value=stamped.old_value,
                    new_value=stamped.new_value,
                    source=stamped.source,
                    effective_at=stamped.effective_at,
                    created_at=stamped.created_at,
                )
            )
            await session.commit()
        return stamped

    async def latest(
        self, *, character_id: str, operator_id: str, direction: str,
    ) -> AddressChangeEvent | None:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(OperatorAddressChangeLogRow)
                    .where(
                        OperatorAddressChangeLogRow.character_id == character_id,
                        OperatorAddressChangeLogRow.operator_id == operator_id,
                        OperatorAddressChangeLogRow.direction == direction,
                    )
                    .order_by(OperatorAddressChangeLogRow.effective_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            return _row_to_domain(row) if row is not None else None

    async def list_for_pair(
        self, *, character_id: str, operator_id: str,
    ) -> list[AddressChangeEvent]:
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(OperatorAddressChangeLogRow)
                    .where(
                        OperatorAddressChangeLogRow.character_id == character_id,
                        OperatorAddressChangeLogRow.operator_id == operator_id,
                    )
                    .order_by(OperatorAddressChangeLogRow.effective_at.desc())
                )
            ).scalars().all()
            return [_row_to_domain(row) for row in rows]


def _row_to_domain(row: OperatorAddressChangeLogRow) -> AddressChangeEvent:
    return AddressChangeEvent(
        id=row.id,
        character_id=row.character_id,
        operator_id=row.operator_id,
        direction=row.direction,
        old_value=row.old_value,
        new_value=row.new_value,
        source=row.source,
        effective_at=row.effective_at,
        created_at=row.created_at,
    )
