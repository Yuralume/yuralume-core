"""Telegram long-polling worker for inbound messaging."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from kokoro_link.application.services.messaging_dispatcher import MessagingDispatcher
from kokoro_link.contracts.messaging import (
    InboundMessage,
    MessagingAccountRepositoryPort,
    ParsedInbound,
    TelegramPollingPort,
)
from kokoro_link.contracts.object_storage import ObjectStoragePort
from kokoro_link.contracts.repositories import CharacterRepositoryPort
from kokoro_link.domain.entities.messaging_account import MessagingAccount

TelegramUpdateParser = Callable[[dict[str, Any]], ParsedInbound | None]
TelegramPhotoDownloader = Callable[
    ...,
    Awaitable[str | None],
]

_LOGGER = logging.getLogger(__name__)
_DUPLICATE_BOT_TOKEN_ERROR = (
    "Duplicate Telegram bot token is bound to multiple polling accounts; "
    "delete duplicate Telegram accounts before polling can run."
)


@dataclass(frozen=True, slots=True)
class TelegramPollingAccountResult:
    account_id: str
    acquired: bool
    updates_seen: int = 0
    dispatched: int = 0
    error: str | None = None


@dataclass(frozen=True, slots=True)
class TelegramPollingSweepResult:
    accounts_seen: int
    acquired: int
    updates_seen: int
    dispatched: int
    errors: tuple[str, ...] = ()


class TelegramPollingService:
    def __init__(
        self,
        *,
        account_repository: MessagingAccountRepositoryPort,
        character_repository: CharacterRepositoryPort,
        dispatcher: MessagingDispatcher,
        polling_client: TelegramPollingPort,
        update_parser: TelegramUpdateParser,
        photo_downloader: TelegramPhotoDownloader,
        uploads_dir: Path,
        object_storage: ObjectStoragePort | None = None,
        owner_id: str | None = None,
        poll_interval_seconds: float = 2.0,
        long_poll_timeout_seconds: int = 25,
        lock_ttl_seconds: int = 90,
        max_concurrency: int = 4,
    ) -> None:
        self._accounts = account_repository
        self._characters = character_repository
        self._dispatcher = dispatcher
        self._polling_client = polling_client
        self._update_parser = update_parser
        self._photo_downloader = photo_downloader
        self._uploads_dir = uploads_dir
        self._object_storage = object_storage
        self._owner_id = owner_id or f"telegram-polling-{uuid4()}"
        self._poll_interval_seconds = poll_interval_seconds
        self._long_poll_timeout_seconds = long_poll_timeout_seconds
        self._lock_ttl = timedelta(seconds=lock_ttl_seconds)
        self._max_concurrency = max(1, max_concurrency)
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self.running:
            return
        self._task = asyncio.create_task(
            self._run(),
            name="telegram-polling-service",
        )

    async def stop(self) -> None:
        if self._task is None:
            return
        task = self._task
        self._task = None
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def poll_once(self) -> TelegramPollingSweepResult:
        accounts = await self._accounts.list_polling_candidates()
        if not accounts:
            return TelegramPollingSweepResult(
                accounts_seen=0, acquired=0, updates_seen=0, dispatched=0,
            )

        pollable_accounts, duplicate_accounts = _split_duplicate_bot_tokens(
            accounts,
        )
        if duplicate_accounts:
            _LOGGER.warning(
                "telegram polling skipped %d accounts with duplicate bot token",
                len(duplicate_accounts),
            )

        semaphore = asyncio.Semaphore(self._max_concurrency)
        results = await asyncio.gather(
            *(
                self._mark_duplicate_bot_token_account(account, semaphore)
                for account in duplicate_accounts
            ),
            *(self._poll_account(account, semaphore) for account in pollable_accounts),
        )
        errors = tuple(r.error for r in results if r.error)
        return TelegramPollingSweepResult(
            accounts_seen=len(accounts),
            acquired=sum(1 for r in results if r.acquired),
            updates_seen=sum(r.updates_seen for r in results),
            dispatched=sum(r.dispatched for r in results),
            errors=errors,
        )

    async def _run(self) -> None:
        while True:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOGGER.exception("telegram polling sweep failed")
            await asyncio.sleep(self._poll_interval_seconds)

    async def _poll_account(
        self,
        account: MessagingAccount,
        semaphore: asyncio.Semaphore,
    ) -> TelegramPollingAccountResult:
        async with semaphore:
            now = _utcnow()
            locked = await self._accounts.try_acquire_polling_lock(
                account.id,
                owner_id=self._owner_id,
                now=now,
                ttl=self._lock_ttl,
            )
            if locked is None:
                return TelegramPollingAccountResult(
                    account_id=account.id, acquired=False,
                )
            try:
                return await self._poll_locked_account(locked)
            finally:
                await self._accounts.release_polling_lock(
                    locked.id, owner_id=self._owner_id,
                )

    async def _mark_duplicate_bot_token_account(
        self,
        account: MessagingAccount,
        semaphore: asyncio.Semaphore,
    ) -> TelegramPollingAccountResult:
        async with semaphore:
            now = _utcnow()
            locked = await self._accounts.try_acquire_polling_lock(
                account.id,
                owner_id=self._owner_id,
                now=now,
                ttl=self._lock_ttl,
            )
            if locked is None:
                return TelegramPollingAccountResult(
                    account_id=account.id, acquired=False,
                    error=_DUPLICATE_BOT_TOKEN_ERROR,
                )
            try:
                await self._record_error(
                    locked, _DUPLICATE_BOT_TOKEN_ERROR, at=now,
                )
                return TelegramPollingAccountResult(
                    account_id=account.id,
                    acquired=True,
                    error=_DUPLICATE_BOT_TOKEN_ERROR,
                )
            finally:
                await self._accounts.release_polling_lock(
                    locked.id, owner_id=self._owner_id,
                )

    async def _poll_locked_account(
        self, account: MessagingAccount,
    ) -> TelegramPollingAccountResult:
        token = account.credentials.get("bot_token", "")
        if not token:
            error = "Missing bot_token"
            await self._record_error(account, error)
            return TelegramPollingAccountResult(
                account_id=account.id, acquired=True, error=error,
            )

        response = await self._polling_client.get_updates(
            bot_token=token,
            offset=account.polling_offset,
            timeout_seconds=self._long_poll_timeout_seconds,
        )
        checked_at = _utcnow()
        if not bool(response.get("ok")):
            error = _telegram_error(response)
            await self._record_error(account, error, at=checked_at)
            return TelegramPollingAccountResult(
                account_id=account.id, acquired=True, error=error,
            )

        raw_updates = response.get("result")
        if not isinstance(raw_updates, list):
            error = "Telegram getUpdates returned an unexpected result shape"
            await self._record_error(account, error, at=checked_at)
            return TelegramPollingAccountResult(
                account_id=account.id, acquired=True, error=error,
            )

        await self._accounts.mark_polling_success(
            account.id, owner_id=self._owner_id, at=checked_at,
        )
        updates_seen = 0
        dispatched = 0
        for raw in _ordered_updates(raw_updates):
            update_id = _update_id(raw)
            if update_id is None:
                continue
            updates_seen += 1
            try:
                parsed = self._update_parser(raw)
                if parsed is not None:
                    inbound = await self._build_inbound(
                        account, parsed, token,
                    )
                    await self._dispatcher.handle_inbound(inbound)
                    dispatched += 1
            except Exception:
                error = f"Telegram update {update_id} processing failed"
                _LOGGER.exception(
                    "telegram polling update failed account=%s update_id=%s",
                    account.id,
                    update_id,
                )
                await self._record_error(account, error)
                return TelegramPollingAccountResult(
                    account_id=account.id,
                    acquired=True,
                    updates_seen=updates_seen,
                    dispatched=dispatched,
                    error=error,
                )
            advanced = await self._accounts.advance_polling_offset(
                account.id,
                owner_id=self._owner_id,
                offset=update_id + 1,
                at=_utcnow(),
            )
            if not advanced:
                return TelegramPollingAccountResult(
                    account_id=account.id,
                    acquired=True,
                    updates_seen=updates_seen,
                    dispatched=dispatched,
                    error="Polling lock was lost before offset advance",
                )

        return TelegramPollingAccountResult(
            account_id=account.id,
            acquired=True,
            updates_seen=updates_seen,
            dispatched=dispatched,
        )

    async def _build_inbound(
        self,
        account: MessagingAccount,
        parsed: ParsedInbound,
        bot_token: str,
    ) -> InboundMessage:
        attachment_urls: tuple[str, ...] = ()
        if parsed.photo_refs:
            owner_id = await self._account_owner_id(account)
            urls: list[str] = []
            for file_id in parsed.photo_refs:
                url = await self._photo_downloader(
                    bot_token=bot_token,
                    file_id=file_id,
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

    async def _account_owner_id(self, account: MessagingAccount) -> str:
        try:
            character = await self._characters.get(account.character_id)
        except Exception:
            _LOGGER.exception(
                "could not resolve owner for messaging account %s", account.id,
            )
            return "default"
        return str(getattr(character, "user_id", "default") or "default")

    async def _record_error(
        self,
        account: MessagingAccount,
        error: str,
        *,
        at: datetime | None = None,
    ) -> None:
        await self._accounts.record_polling_error(
            account.id,
            owner_id=self._owner_id,
            error=error,
            at=at or _utcnow(),
        )


def _ordered_updates(raw_updates: list[Any]) -> list[dict[str, Any]]:
    updates = [raw for raw in raw_updates if isinstance(raw, dict)]
    return sorted(updates, key=lambda raw: _update_id(raw) or -1)


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
    pollable: list[MessagingAccount] = []
    duplicates: list[MessagingAccount] = []
    for account in accounts:
        if account.id in duplicate_ids:
            duplicates.append(account)
        else:
            pollable.append(account)
    return pollable, duplicates


def _update_id(update: dict[str, Any]) -> int | None:
    raw = update.get("update_id")
    return raw if isinstance(raw, int) else None


def _telegram_error(response: dict[str, Any]) -> str:
    raw = response.get("description") or response.get("error")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()[:1000]
    return "Telegram getUpdates failed"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)
