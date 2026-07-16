"""SQLAlchemy messaging account repository."""

from datetime import datetime, timedelta

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker

from kokoro_link.contracts.messaging import MessagingAccountRepositoryPort
from kokoro_link.domain.entities.messaging_account import MessagingAccount
from kokoro_link.domain.value_objects.delivery_mode import DeliveryMode
from kokoro_link.domain.value_objects.platform import Platform
from kokoro_link.infrastructure.persistence.models import MessagingAccountRow
from kokoro_link.infrastructure.persistence.sa_messaging_account_mapping import (
    apply_domain_to_row,
    row_to_domain,
)


class SAMessagingAccountRepository(MessagingAccountRepositoryPort):
    def __init__(self, session_factory: sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get(self, account_id: str) -> MessagingAccount | None:
        async with self._session_factory() as session:
            row = await session.get(MessagingAccountRow, account_id)
            return row_to_domain(row) if row is not None else None

    async def find_by_slug(self, webhook_slug: str) -> MessagingAccount | None:
        async with self._session_factory() as session:
            result = await session.execute(
                select(MessagingAccountRow).where(
                    MessagingAccountRow.webhook_slug == webhook_slug,
                ),
            )
            row = result.scalar_one_or_none()
            return row_to_domain(row) if row is not None else None

    async def find_for_character(
        self, platform: Platform, character_id: str,
    ) -> MessagingAccount | None:
        async with self._session_factory() as session:
            result = await session.execute(
                select(MessagingAccountRow).where(
                    MessagingAccountRow.platform == platform.value,
                    MessagingAccountRow.character_id == character_id,
                ),
            )
            row = result.scalar_one_or_none()
            return row_to_domain(row) if row is not None else None

    async def list_for_character(
        self, character_id: str,
    ) -> list[MessagingAccount]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(MessagingAccountRow)
                .where(MessagingAccountRow.character_id == character_id)
                .order_by(
                    MessagingAccountRow.platform, MessagingAccountRow.created_at,
                ),
            )
            return [row_to_domain(r) for r in result.scalars().all()]

    async def list_all(self) -> list[MessagingAccount]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(MessagingAccountRow).order_by(MessagingAccountRow.created_at),
            )
            return [row_to_domain(r) for r in result.scalars().all()]

    async def list_polling_candidates(self) -> list[MessagingAccount]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(MessagingAccountRow)
                .where(
                    MessagingAccountRow.enabled.is_(True),
                    MessagingAccountRow.platform == Platform.TELEGRAM.value,
                    MessagingAccountRow.delivery_mode == DeliveryMode.POLLING.value,
                )
                .order_by(MessagingAccountRow.created_at),
            )
            return [row_to_domain(r) for r in result.scalars().all()]

    async def list_gateway_candidates(self) -> list[MessagingAccount]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(MessagingAccountRow)
                .where(
                    MessagingAccountRow.enabled.is_(True),
                    MessagingAccountRow.delivery_mode == DeliveryMode.GATEWAY.value,
                )
                .order_by(MessagingAccountRow.created_at),
            )
            return [row_to_domain(r) for r in result.scalars().all()]

    async def save(self, account: MessagingAccount) -> None:
        async with self._session_factory() as session:
            row = await session.get(MessagingAccountRow, account.id)
            if row is None:
                row = MessagingAccountRow(id=account.id)
                session.add(row)
            apply_domain_to_row(account, row)
            await session.commit()

    async def try_acquire_polling_lock(
        self,
        account_id: str,
        *,
        owner_id: str,
        now: datetime,
        ttl: timedelta,
    ) -> MessagingAccount | None:
        until = now + ttl
        async with self._session_factory() as session:
            result = await session.execute(
                update(MessagingAccountRow)
                .where(
                    MessagingAccountRow.id == account_id,
                    MessagingAccountRow.enabled.is_(True),
                    MessagingAccountRow.platform == Platform.TELEGRAM.value,
                    MessagingAccountRow.delivery_mode == DeliveryMode.POLLING.value,
                    or_(
                        MessagingAccountRow.polling_lock_owner == owner_id,
                        MessagingAccountRow.polling_lock_until.is_(None),
                        MessagingAccountRow.polling_lock_until <= now,
                    ),
                )
                .values(
                    polling_lock_owner=owner_id,
                    polling_lock_until=until,
                    updated_at=now,
                ),
            )
            if result.rowcount != 1:
                await session.rollback()
                return None
            row = await session.get(MessagingAccountRow, account_id)
            await session.commit()
            return row_to_domain(row) if row is not None else None

    async def try_acquire_gateway_lock(
        self,
        account_id: str,
        *,
        owner_id: str,
        now: datetime,
        ttl: timedelta,
    ) -> MessagingAccount | None:
        until = now + ttl
        async with self._session_factory() as session:
            result = await session.execute(
                update(MessagingAccountRow)
                .where(
                    MessagingAccountRow.id == account_id,
                    MessagingAccountRow.enabled.is_(True),
                    MessagingAccountRow.delivery_mode == DeliveryMode.GATEWAY.value,
                    or_(
                        MessagingAccountRow.polling_lock_owner == owner_id,
                        MessagingAccountRow.polling_lock_until.is_(None),
                        MessagingAccountRow.polling_lock_until <= now,
                    ),
                )
                .values(
                    polling_lock_owner=owner_id,
                    polling_lock_until=until,
                    updated_at=now,
                ),
            )
            if result.rowcount != 1:
                await session.rollback()
                return None
            row = await session.get(MessagingAccountRow, account_id)
            await session.commit()
            return row_to_domain(row) if row is not None else None

    async def release_polling_lock(
        self, account_id: str, *, owner_id: str,
    ) -> bool:
        async with self._session_factory() as session:
            result = await session.execute(
                update(MessagingAccountRow)
                .where(
                    MessagingAccountRow.id == account_id,
                    MessagingAccountRow.polling_lock_owner == owner_id,
                )
                .values(
                    polling_lock_owner=None,
                    polling_lock_until=None,
                ),
            )
            await session.commit()
            return result.rowcount == 1

    async def release_gateway_lock(
        self, account_id: str, *, owner_id: str,
    ) -> bool:
        return await self.release_polling_lock(account_id, owner_id=owner_id)

    async def advance_polling_offset(
        self,
        account_id: str,
        *,
        owner_id: str,
        offset: int,
        at: datetime,
    ) -> bool:
        async with self._session_factory() as session:
            result = await session.execute(
                update(MessagingAccountRow)
                .where(
                    MessagingAccountRow.id == account_id,
                    MessagingAccountRow.polling_lock_owner == owner_id,
                )
                .values(
                    polling_offset=offset,
                    polling_last_update_at=at,
                    polling_last_error=None,
                    updated_at=at,
                ),
            )
            await session.commit()
            return result.rowcount == 1

    async def mark_polling_success(
        self,
        account_id: str,
        *,
        owner_id: str,
        at: datetime,
    ) -> bool:
        async with self._session_factory() as session:
            result = await session.execute(
                update(MessagingAccountRow)
                .where(
                    MessagingAccountRow.id == account_id,
                    MessagingAccountRow.polling_lock_owner == owner_id,
                )
                .values(
                    polling_last_update_at=at,
                    polling_last_error=None,
                    updated_at=at,
                ),
            )
            await session.commit()
            return result.rowcount == 1

    async def mark_gateway_success(
        self,
        account_id: str,
        *,
        owner_id: str,
        at: datetime,
    ) -> bool:
        return await self.mark_polling_success(
            account_id, owner_id=owner_id, at=at,
        )

    async def record_polling_error(
        self,
        account_id: str,
        *,
        owner_id: str,
        error: str,
        at: datetime,
    ) -> bool:
        async with self._session_factory() as session:
            result = await session.execute(
                update(MessagingAccountRow)
                .where(
                    MessagingAccountRow.id == account_id,
                    MessagingAccountRow.polling_lock_owner == owner_id,
                )
                .values(
                    polling_last_update_at=at,
                    polling_last_error=error[:1000],
                    updated_at=at,
                ),
            )
            await session.commit()
            return result.rowcount == 1

    async def record_gateway_error(
        self,
        account_id: str,
        *,
        owner_id: str,
        error: str,
        at: datetime,
    ) -> bool:
        return await self.record_polling_error(
            account_id, owner_id=owner_id, error=error, at=at,
        )

    async def delete(self, account_id: str) -> bool:
        async with self._session_factory() as session:
            row = await session.get(MessagingAccountRow, account_id)
            if row is None:
                return False
            await session.delete(row)
            await session.commit()
            return True

    async def delete_for_character(self, character_id: str) -> int:
        async with self._session_factory() as session:
            result = await session.execute(
                select(MessagingAccountRow).where(
                    MessagingAccountRow.character_id == character_id,
                ),
            )
            rows = result.scalars().all()
            for row in rows:
                await session.delete(row)
            await session.commit()
            return len(rows)
