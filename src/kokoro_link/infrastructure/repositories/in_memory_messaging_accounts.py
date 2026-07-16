"""In-process messaging account repository for dev/tests."""

from datetime import datetime, timedelta

from kokoro_link.contracts.messaging import MessagingAccountRepositoryPort
from kokoro_link.domain.entities.messaging_account import MessagingAccount
from kokoro_link.domain.value_objects.delivery_mode import DeliveryMode
from kokoro_link.domain.value_objects.platform import Platform


class InMemoryMessagingAccountRepository(MessagingAccountRepositoryPort):
    def __init__(self) -> None:
        self._by_id: dict[str, MessagingAccount] = {}

    async def get(self, account_id: str) -> MessagingAccount | None:
        return self._by_id.get(account_id)

    async def find_by_slug(self, webhook_slug: str) -> MessagingAccount | None:
        for account in self._by_id.values():
            if account.webhook_slug == webhook_slug:
                return account
        return None

    async def find_for_character(
        self, platform: Platform, character_id: str,
    ) -> MessagingAccount | None:
        for account in self._by_id.values():
            if (
                account.platform == platform
                and account.character_id == character_id
            ):
                return account
        return None

    async def list_for_character(
        self, character_id: str,
    ) -> list[MessagingAccount]:
        items = [
            a for a in self._by_id.values() if a.character_id == character_id
        ]
        items.sort(key=lambda a: (a.platform.value, a.created_at))
        return items

    async def list_all(self) -> list[MessagingAccount]:
        return sorted(self._by_id.values(), key=lambda a: a.created_at)

    async def list_polling_candidates(self) -> list[MessagingAccount]:
        items = [
            a for a in self._by_id.values()
            if (
                a.enabled
                and a.platform == Platform.TELEGRAM
                and a.delivery_mode == DeliveryMode.POLLING
            )
        ]
        return sorted(items, key=lambda a: a.created_at)

    async def list_gateway_candidates(self) -> list[MessagingAccount]:
        items = [
            a for a in self._by_id.values()
            if (
                a.enabled
                and a.delivery_mode == DeliveryMode.GATEWAY
            )
        ]
        return sorted(items, key=lambda a: a.created_at)

    async def save(self, account: MessagingAccount) -> None:
        self._by_id[account.id] = account

    async def try_acquire_polling_lock(
        self,
        account_id: str,
        *,
        owner_id: str,
        now: datetime,
        ttl: timedelta,
    ) -> MessagingAccount | None:
        account = self._by_id.get(account_id)
        if account is None or not _can_poll(account):
            return None
        locked_by_other = (
            account.polling_lock_owner
            and account.polling_lock_owner != owner_id
            and account.polling_lock_until is not None
            and account.polling_lock_until > now
        )
        if locked_by_other:
            return None
        updated = account.with_polling_lock(
            owner=owner_id, until=now + ttl, now=now,
        )
        self._by_id[account_id] = updated
        return updated

    async def try_acquire_gateway_lock(
        self,
        account_id: str,
        *,
        owner_id: str,
        now: datetime,
        ttl: timedelta,
    ) -> MessagingAccount | None:
        account = self._by_id.get(account_id)
        if account is None or not _can_use_gateway(account):
            return None
        locked_by_other = (
            account.polling_lock_owner
            and account.polling_lock_owner != owner_id
            and account.polling_lock_until is not None
            and account.polling_lock_until > now
        )
        if locked_by_other:
            return None
        updated = account.with_polling_lock(
            owner=owner_id, until=now + ttl, now=now,
        )
        self._by_id[account_id] = updated
        return updated

    async def release_polling_lock(
        self, account_id: str, *, owner_id: str,
    ) -> bool:
        account = self._by_id.get(account_id)
        if account is None or account.polling_lock_owner != owner_id:
            return False
        self._by_id[account_id] = account.with_polling_lock(
            owner=None, until=None,
        )
        return True

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
        account = self._by_id.get(account_id)
        if account is None or account.polling_lock_owner != owner_id:
            return False
        self._by_id[account_id] = account.with_polling_progress(
            offset=offset, checked_at=at, error=None, now=at,
        )
        return True

    async def mark_polling_success(
        self,
        account_id: str,
        *,
        owner_id: str,
        at: datetime,
    ) -> bool:
        account = self._by_id.get(account_id)
        if account is None or account.polling_lock_owner != owner_id:
            return False
        self._by_id[account_id] = account.with_polling_progress(
            checked_at=at, error=None, now=at,
        )
        return True

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
        account = self._by_id.get(account_id)
        if account is None or account.polling_lock_owner != owner_id:
            return False
        self._by_id[account_id] = account.with_polling_progress(
            checked_at=at, error=error, now=at,
        )
        return True

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
        return self._by_id.pop(account_id, None) is not None

    async def delete_for_character(self, character_id: str) -> int:
        victims = [
            aid for aid, a in self._by_id.items() if a.character_id == character_id
        ]
        for aid in victims:
            del self._by_id[aid]
        return len(victims)


def _can_poll(account: MessagingAccount) -> bool:
    return (
        account.enabled
        and account.platform == Platform.TELEGRAM
        and account.delivery_mode == DeliveryMode.POLLING
    )


def _can_use_gateway(account: MessagingAccount) -> bool:
    return (
        account.enabled
        and account.delivery_mode == DeliveryMode.GATEWAY
    )
