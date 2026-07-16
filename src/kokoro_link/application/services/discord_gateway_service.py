"""Discord Gateway worker for inbound messaging."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from kokoro_link.application.services.messaging_dispatcher import MessagingDispatcher
from kokoro_link.contracts.messaging import (
    InboundMessage,
    MessagingAccountRepositoryPort,
    ParsedInbound,
)
from kokoro_link.contracts.object_storage import ObjectStoragePort
from kokoro_link.contracts.repositories import CharacterRepositoryPort
from kokoro_link.domain.entities.messaging_account import MessagingAccount
from kokoro_link.domain.value_objects.delivery_mode import DeliveryMode
from kokoro_link.domain.value_objects.platform import Platform

DiscordMessageParser = Callable[..., ParsedInbound | None]
DiscordAttachmentDownloader = Callable[..., Awaitable[str | None]]

_LOGGER = logging.getLogger(__name__)
_DUPLICATE_BOT_TOKEN_ERROR = (
    "Duplicate Discord bot token is bound to multiple gateway accounts; "
    "delete duplicate Discord accounts before Gateway can run."
)


class DiscordGatewayClientPort(Protocol):
    async def connect(
        self,
        *,
        bot_token: str,
        on_message_create: Callable[[dict[str, Any], str | None], Awaitable[None]],
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class DiscordGatewaySyncResult:
    accounts_seen: int
    tasks_running: int
    duplicates: int = 0


class DiscordGatewayService:
    def __init__(
        self,
        *,
        account_repository: MessagingAccountRepositoryPort,
        character_repository: CharacterRepositoryPort,
        dispatcher: MessagingDispatcher,
        gateway_client: DiscordGatewayClientPort,
        message_parser: DiscordMessageParser,
        attachment_downloader: DiscordAttachmentDownloader,
        uploads_dir: Path,
        object_storage: ObjectStoragePort | None = None,
        owner_id: str | None = None,
        sync_interval_seconds: float = 5.0,
        reconnect_delay_seconds: float = 5.0,
        lock_ttl_seconds: int = 90,
    ) -> None:
        self._accounts = account_repository
        self._characters = character_repository
        self._dispatcher = dispatcher
        self._gateway_client = gateway_client
        self._message_parser = message_parser
        self._attachment_downloader = attachment_downloader
        self._uploads_dir = uploads_dir
        self._object_storage = object_storage
        self._owner_id = owner_id or f"discord-gateway-{uuid4()}"
        self._sync_interval_seconds = sync_interval_seconds
        self._reconnect_delay_seconds = reconnect_delay_seconds
        self._lock_ttl = timedelta(seconds=lock_ttl_seconds)
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self.running:
            return
        self._task = asyncio.create_task(
            self._run(),
            name="discord-gateway-service",
        )

    async def stop(self) -> None:
        if self._task is not None:
            task = self._task
            self._task = None
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self._cancel_all_account_tasks()

    async def sync_once(self) -> DiscordGatewaySyncResult:
        accounts = [
            account for account in await self._accounts.list_gateway_candidates()
            if _is_gateway_account(account)
        ]
        pollable_accounts, duplicate_accounts = _split_duplicate_bot_tokens(accounts)
        desired_ids = {account.id for account in pollable_accounts}

        for account_id, task in list(self._tasks.items()):
            if task.done():
                self._tasks.pop(account_id, None)
                continue
            if account_id not in desired_ids:
                task.cancel()
                self._tasks.pop(account_id, None)

        for account in duplicate_accounts:
            await self._mark_duplicate_bot_token_account(account)

        for account in pollable_accounts:
            existing = self._tasks.get(account.id)
            if existing is None or existing.done():
                self._tasks[account.id] = asyncio.create_task(
                    self._run_account_loop(account.id),
                    name=f"discord-gateway-account-{account.id}",
                )

        return DiscordGatewaySyncResult(
            accounts_seen=len(accounts),
            tasks_running=len(self._tasks),
            duplicates=len(duplicate_accounts),
        )

    async def _run(self) -> None:
        while True:
            try:
                await self.sync_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOGGER.exception("discord gateway sync failed")
            await asyncio.sleep(self._sync_interval_seconds)

    async def _run_account_loop(self, account_id: str) -> None:
        while True:
            account = await self._accounts.get(account_id)
            if not _is_gateway_account(account):
                return
            locked = await self._accounts.try_acquire_gateway_lock(
                account_id,
                owner_id=self._owner_id,
                now=_utcnow(),
                ttl=self._lock_ttl,
            )
            if locked is None:
                return

            current_task = asyncio.current_task()
            refresher = asyncio.create_task(
                self._refresh_lock_until_cancelled(locked.id, current_task),
                name=f"discord-gateway-lock-{locked.id}",
            )
            try:
                await self._connect_locked_account(locked)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _LOGGER.exception(
                    "discord gateway account failed account=%s", locked.id,
                )
                await self._record_error(locked, str(exc) or exc.__class__.__name__)
            finally:
                refresher.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await refresher
                await self._accounts.release_gateway_lock(
                    locked.id,
                    owner_id=self._owner_id,
                )
            await asyncio.sleep(self._reconnect_delay_seconds)

    async def _connect_locked_account(self, account: MessagingAccount) -> None:
        token = account.credentials.get("bot_token", "")
        if not token:
            await self._record_error(account, "Missing bot_token")
            return
        await self._accounts.mark_gateway_success(
            account.id,
            owner_id=self._owner_id,
            at=_utcnow(),
        )

        async def handle_message(
            raw: dict[str, Any],
            bot_user_id: str | None,
        ) -> None:
            parsed = self._message_parser(raw, bot_user_id=bot_user_id)
            if parsed is None:
                return
            inbound = await self._build_inbound(account, parsed)
            await self._dispatcher.handle_inbound(inbound)
            await self._accounts.mark_gateway_success(
                account.id,
                owner_id=self._owner_id,
                at=_utcnow(),
            )

        await self._gateway_client.connect(
            bot_token=token,
            on_message_create=handle_message,
        )

    async def _build_inbound(
        self,
        account: MessagingAccount,
        parsed: ParsedInbound,
    ) -> InboundMessage:
        attachment_urls: tuple[str, ...] = ()
        if parsed.photo_refs:
            owner_id = await self._account_owner_id(account)
            urls: list[str] = []
            for attachment_url in parsed.photo_refs:
                url = await self._attachment_downloader(
                    attachment_url=attachment_url,
                    uploads_dir=self._uploads_dir,
                    object_storage=self._object_storage,
                    user_id=owner_id,
                )
                if url:
                    urls.append(url)
            attachment_urls = tuple(urls)
        return InboundMessage.from_parsed(
            parsed,
            account_id=account.id,
            attachment_urls=attachment_urls,
        )

    async def _refresh_lock_until_cancelled(
        self,
        account_id: str,
        account_task: asyncio.Task[None] | None,
    ) -> None:
        delay = max(1.0, self._lock_ttl.total_seconds() / 3.0)
        while True:
            await asyncio.sleep(delay)
            refreshed = await self._accounts.try_acquire_gateway_lock(
                account_id,
                owner_id=self._owner_id,
                now=_utcnow(),
                ttl=self._lock_ttl,
            )
            if refreshed is None:
                _LOGGER.warning("discord gateway lock lost account=%s", account_id)
                if account_task is not None:
                    account_task.cancel()
                return

    async def _mark_duplicate_bot_token_account(
        self,
        account: MessagingAccount,
    ) -> None:
        locked = await self._accounts.try_acquire_gateway_lock(
            account.id,
            owner_id=self._owner_id,
            now=_utcnow(),
            ttl=self._lock_ttl,
        )
        if locked is None:
            return
        try:
            await self._record_error(locked, _DUPLICATE_BOT_TOKEN_ERROR)
        finally:
            await self._accounts.release_gateway_lock(
                locked.id, owner_id=self._owner_id,
            )

    async def _record_error(
        self,
        account: MessagingAccount,
        error: str,
    ) -> None:
        await self._accounts.record_gateway_error(
            account.id,
            owner_id=self._owner_id,
            error=error[:1000],
            at=_utcnow(),
        )

    async def _account_owner_id(self, account: MessagingAccount) -> str:
        try:
            character = await self._characters.get(account.character_id)
        except Exception:
            _LOGGER.exception(
                "could not resolve owner for messaging account %s", account.id,
            )
            return "default"
        return str(getattr(character, "user_id", "default") or "default")

    async def _cancel_all_account_tasks(self) -> None:
        tasks = list(self._tasks.values())
        self._tasks.clear()
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task


def _split_duplicate_bot_tokens(
    accounts: list[MessagingAccount],
) -> tuple[list[MessagingAccount], list[MessagingAccount]]:
    by_token: dict[str, list[MessagingAccount]] = {}
    for account in accounts:
        token = account.credentials.get("bot_token", "")
        if token:
            by_token.setdefault(token, []).append(account)

    duplicate_ids = {
        account.id
        for group in by_token.values()
        if len(group) > 1
        for account in group
    }
    if not duplicate_ids:
        return accounts, []
    runnable: list[MessagingAccount] = []
    duplicates: list[MessagingAccount] = []
    for account in accounts:
        if account.id in duplicate_ids:
            duplicates.append(account)
        else:
            runnable.append(account)
    return runnable, duplicates


def _is_gateway_account(account: MessagingAccount | None) -> bool:
    return (
        account is not None
        and account.enabled
        and account.platform == Platform.DISCORD
        and account.delivery_mode == DeliveryMode.GATEWAY
    )


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)
