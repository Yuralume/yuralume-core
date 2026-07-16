"""Channel binding CRUD under a messaging account.

Uniqueness inside an account is simple: one chat_ref per binding
(``UNIQUE(account_id, chat_ref)`` at the DB layer). Cross-account
sharing is allowed — two characters can technically both have a bot in
the same group, each tracked under its own account.
"""

from kokoro_link.contracts.messaging import (
    ChannelBindingRepositoryPort,
    MessagingAccountRepositoryPort,
)
from kokoro_link.domain.entities.channel_binding import ChannelBinding


class ChannelBindingConflictError(Exception):
    """Raised when a binding would violate the per-account chat uniqueness."""


class ChannelBindingService:
    def __init__(
        self,
        *,
        binding_repository: ChannelBindingRepositoryPort,
        account_repository: MessagingAccountRepositoryPort,
    ) -> None:
        self._bindings = binding_repository
        self._accounts = account_repository

    async def create(
        self,
        *,
        account_id: str,
        chat_ref: str,
        enabled: bool = True,
        accepts_proactive: bool = False,
    ) -> ChannelBinding:
        account = await self._accounts.get(account_id)
        if account is None:
            raise ValueError("Account not found")
        if await self._bindings.find(account_id, chat_ref) is not None:
            raise ChannelBindingConflictError(
                f"Account {account_id} already has a binding for chat {chat_ref!r}",
            )
        binding = ChannelBinding.create(
            account_id=account_id,
            chat_ref=chat_ref,
            enabled=enabled,
            accepts_proactive=accepts_proactive,
        )
        await self._bindings.save(binding)
        return binding

    async def update(
        self,
        binding_id: str,
        *,
        enabled: bool | None = None,
        accepts_proactive: bool | None = None,
    ) -> ChannelBinding:
        binding = await self._bindings.get(binding_id)
        if binding is None:
            raise ValueError("Binding not found")
        updated = binding
        if enabled is not None:
            updated = updated.with_enabled(enabled)
        if accepts_proactive is not None:
            updated = updated.with_accepts_proactive(accepts_proactive)
        if updated is not binding:
            await self._bindings.save(updated)
        return updated

    async def set_enabled(self, binding_id: str, enabled: bool) -> ChannelBinding:
        return await self.update(binding_id, enabled=enabled)

    async def delete(self, binding_id: str) -> bool:
        return await self._bindings.delete(binding_id)

    async def list_for_account(self, account_id: str) -> list[ChannelBinding]:
        return await self._bindings.list_for_account(account_id)

    async def get(self, binding_id: str) -> ChannelBinding | None:
        """Fetch a single binding by id. Returns ``None`` when missing.

        Added for the multi-user auth review (P0-1 follow-up): the
        ownership guard layer needs to resolve binding → account →
        character before letting the request through, and reaching for
        the private repo from the route layer would couple the routes
        to persistence shape."""
        return await self._bindings.get(binding_id)
