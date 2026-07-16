"""SQLAlchemy memoir pin repository.

See ``docs/MEMOIR_PLAN.md`` for the design rationale and
``src/kokoro_link/contracts/memoir.py`` for the port spec.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker

from kokoro_link.contracts.memoir import MemoirPinRepositoryPort
from kokoro_link.domain.entities.memoir_pin import MemoirPin
from kokoro_link.infrastructure.persistence.models import MemoirPinRow


def _ensure_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _row_to_domain(row: MemoirPinRow) -> MemoirPin:
    return MemoirPin(
        id=row.id,
        character_id=row.character_id,
        operator_id=row.operator_id,
        entry_kind=row.entry_kind,
        entry_id=row.entry_id,
        pinned_at=_ensure_utc(row.pinned_at),
        created_at=_ensure_utc(row.created_at),
    )


class SAMemoirPinRepository(MemoirPinRepositoryPort):
    def __init__(self, session_factory: sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def list_for(
        self, character_id: str, operator_id: str,
    ) -> list[MemoirPin]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(MemoirPinRow)
                .where(
                    MemoirPinRow.character_id == character_id,
                    MemoirPinRow.operator_id == operator_id,
                )
                .order_by(MemoirPinRow.pinned_at.desc()),
            )
            return [_row_to_domain(r) for r in result.scalars().all()]

    async def add(self, pin: MemoirPin) -> MemoirPin:
        async with self._session_factory() as session:
            existing_row = (await session.execute(
                select(MemoirPinRow).where(
                    MemoirPinRow.character_id == pin.character_id,
                    MemoirPinRow.operator_id == pin.operator_id,
                    MemoirPinRow.entry_kind == pin.entry_kind,
                    MemoirPinRow.entry_id == pin.entry_id,
                ),
            )).scalars().first()
            if existing_row is not None:
                return _row_to_domain(existing_row)
            row = MemoirPinRow(
                id=pin.id,
                character_id=pin.character_id,
                operator_id=pin.operator_id,
                entry_kind=pin.entry_kind,
                entry_id=pin.entry_id,
                pinned_at=pin.pinned_at,
                created_at=pin.created_at,
            )
            session.add(row)
            await session.commit()
            return pin

    async def remove(
        self,
        character_id: str,
        operator_id: str,
        entry_kind: str,
        entry_id: str,
    ) -> bool:
        async with self._session_factory() as session:
            result = await session.execute(
                delete(MemoirPinRow).where(
                    MemoirPinRow.character_id == character_id,
                    MemoirPinRow.operator_id == operator_id,
                    MemoirPinRow.entry_kind == entry_kind,
                    MemoirPinRow.entry_id == entry_id,
                ),
            )
            await session.commit()
            return bool(result.rowcount)

    async def count_for(
        self, character_id: str, operator_id: str,
    ) -> int:
        async with self._session_factory() as session:
            result = await session.execute(
                select(func.count(MemoirPinRow.id)).where(
                    MemoirPinRow.character_id == character_id,
                    MemoirPinRow.operator_id == operator_id,
                ),
            )
            return int(result.scalar_one() or 0)

    async def delete_for_character(self, character_id: str) -> int:
        async with self._session_factory() as session:
            result = await session.execute(
                delete(MemoirPinRow).where(
                    MemoirPinRow.character_id == character_id,
                ),
            )
            await session.commit()
            return int(result.rowcount or 0)
