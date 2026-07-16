"""SQLAlchemy channel binding repository."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker

from kokoro_link.contracts.messaging import ChannelBindingRepositoryPort
from kokoro_link.domain.entities.channel_binding import ChannelBinding
from kokoro_link.infrastructure.persistence.models import ChannelBindingRow
from kokoro_link.infrastructure.persistence.sa_channel_binding_mapping import (
    apply_domain_to_row,
    row_to_domain,
)


class SAChannelBindingRepository(ChannelBindingRepositoryPort):
    def __init__(self, session_factory: sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get(self, binding_id: str) -> ChannelBinding | None:
        async with self._session_factory() as session:
            row = await session.get(ChannelBindingRow, binding_id)
            return row_to_domain(row) if row is not None else None

    async def find(
        self, account_id: str, chat_ref: str,
    ) -> ChannelBinding | None:
        async with self._session_factory() as session:
            result = await session.execute(
                select(ChannelBindingRow).where(
                    ChannelBindingRow.account_id == account_id,
                    ChannelBindingRow.chat_ref == chat_ref,
                ),
            )
            row = result.scalar_one_or_none()
            return row_to_domain(row) if row is not None else None

    async def list_for_account(self, account_id: str) -> list[ChannelBinding]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(ChannelBindingRow)
                .where(ChannelBindingRow.account_id == account_id)
                .order_by(ChannelBindingRow.created_at),
            )
            return [row_to_domain(r) for r in result.scalars().all()]

    async def save(self, binding: ChannelBinding) -> None:
        async with self._session_factory() as session:
            row = await session.get(ChannelBindingRow, binding.id)
            if row is None:
                row = ChannelBindingRow(id=binding.id)
                session.add(row)
            apply_domain_to_row(binding, row)
            await session.commit()

    async def delete(self, binding_id: str) -> bool:
        async with self._session_factory() as session:
            row = await session.get(ChannelBindingRow, binding_id)
            if row is None:
                return False
            await session.delete(row)
            await session.commit()
            return True

    async def delete_for_account(self, account_id: str) -> int:
        async with self._session_factory() as session:
            result = await session.execute(
                select(ChannelBindingRow).where(
                    ChannelBindingRow.account_id == account_id,
                ),
            )
            rows = result.scalars().all()
            for row in rows:
                await session.delete(row)
            await session.commit()
            return len(rows)
