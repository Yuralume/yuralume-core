"""WhatsApp sidecar gateway worker for inbound messaging."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol
from uuid import uuid4

from kokoro_link.application.services.messaging_dispatcher import MessagingDispatcher
from kokoro_link.contracts.messaging import (
    InboundMessage,
    MessagingAccountRepositoryPort,
    ParsedInbound,
)
from kokoro_link.domain.entities.messaging_account import MessagingAccount
from kokoro_link.domain.value_objects.delivery_mode import DeliveryMode
from kokoro_link.domain.value_objects.platform import Platform

WhatsAppEventParser = Callable[[dict[str, Any]], ParsedInbound | None]

_LOGGER = logging.getLogger(__name__)
_DUPLICATE_SESSION_ERROR = (
    "Duplicate WhatsApp sidecar session is bound to multiple gateway accounts; "
    "delete duplicate WhatsApp accounts before Gateway can run."
)


class WhatsAppSidecarClientPort(Protocol):
    async def connect(
        self,
        *,
        sidecar_url: str,
        session_id: str,
        api_token: str | None,
        on_event: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class WhatsAppGatewaySyncResult:
    accounts_seen: int
    tasks_running: int
    duplicates: int = 0


class WhatsAppGatewayService:
    def __init__(
        self,
        *,
        account_repository: MessagingAccountRepositoryPort,
        dispatcher: MessagingDispatcher,
        sidecar_client: WhatsAppSidecarClientPort,
        event_parser: WhatsAppEventParser,
        owner_id: str | None = None,
        sync_interval_seconds: float = 5.0,
        reconnect_delay_seconds: float = 5.0,
        lock_ttl_seconds: int = 90,
    ) -> None:
        self._accounts = account_repository
        self._dispatcher = dispatcher
        self._sidecar_client = sidecar_client
        self._event_parser = event_parser
        self._owner_id = owner_id or f"whatsapp-gateway-{uuid4()}"
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
            name="whatsapp-gateway-service",
        )

    async def stop(self) -> None:
        if self._task is not None:
            task = self._task
            self._task = None
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self._cancel_all_account_tasks()

    async def sync_once(self) -> WhatsAppGatewaySyncResult:
        accounts = [
            account for account in await self._accounts.list_gateway_candidates()
            if _is_gateway_account(account)
        ]
        runnable_accounts, duplicate_accounts = _split_duplicate_sessions(accounts)
        desired_ids = {account.id for account in runnable_accounts}

        for account_id, task in list(self._tasks.items()):
            if task.done():
                self._tasks.pop(account_id, None)
                continue
            if account_id not in desired_ids:
                task.cancel()
                self._tasks.pop(account_id, None)

        for account in duplicate_accounts:
            await self._mark_duplicate_session_account(account)

        for account in runnable_accounts:
            existing = self._tasks.get(account.id)
            if existing is None or existing.done():
                self._tasks[account.id] = asyncio.create_task(
                    self._run_account_loop(account.id),
                    name=f"whatsapp-gateway-account-{account.id}",
                )

        return WhatsAppGatewaySyncResult(
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
                _LOGGER.exception("whatsapp gateway sync failed")
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
                name=f"whatsapp-gateway-lock-{locked.id}",
            )
            try:
                await self._connect_locked_account(locked)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _LOGGER.exception(
                    "whatsapp gateway account failed account=%s", locked.id,
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
        sidecar_url = account.credentials.get("sidecar_url", "").strip().rstrip("/")
        session_id = account.credentials.get("session_id", "").strip()
        api_token = account.credentials.get("api_token", "").strip() or None
        if not sidecar_url or not session_id:
            await self._record_error(account, "Missing sidecar_url/session_id")
            return

        await self._accounts.mark_gateway_success(
            account.id,
            owner_id=self._owner_id,
            at=_utcnow(),
        )

        async def handle_event(raw: dict[str, Any]) -> None:
            parsed = self._event_parser(raw)
            if parsed is None:
                return
            inbound = InboundMessage.from_parsed(
                parsed,
                account_id=account.id,
                attachment_urls=parsed.photo_refs,
            )
            await self._dispatcher.handle_inbound(inbound)
            await self._accounts.mark_gateway_success(
                account.id,
                owner_id=self._owner_id,
                at=_utcnow(),
            )

        await self._sidecar_client.connect(
            sidecar_url=sidecar_url,
            session_id=session_id,
            api_token=api_token,
            on_event=handle_event,
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
                _LOGGER.warning("whatsapp gateway lock lost account=%s", account_id)
                if account_task is not None:
                    account_task.cancel()
                return

    async def _mark_duplicate_session_account(
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
            await self._record_error(locked, _DUPLICATE_SESSION_ERROR)
        finally:
            await self._accounts.release_gateway_lock(
                locked.id,
                owner_id=self._owner_id,
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

    async def _cancel_all_account_tasks(self) -> None:
        tasks = list(self._tasks.values())
        self._tasks.clear()
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task


def _split_duplicate_sessions(
    accounts: list[MessagingAccount],
) -> tuple[list[MessagingAccount], list[MessagingAccount]]:
    by_session: dict[tuple[str, str], list[MessagingAccount]] = {}
    for account in accounts:
        session_key = _session_key(account)
        if session_key is not None:
            by_session.setdefault(session_key, []).append(account)

    duplicate_ids = {
        account.id
        for group in by_session.values()
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


def _session_key(account: MessagingAccount) -> tuple[str, str] | None:
    sidecar_url = account.credentials.get("sidecar_url", "").strip().rstrip("/")
    session_id = account.credentials.get("session_id", "").strip()
    if not sidecar_url or not session_id:
        return None
    return (sidecar_url, session_id)


def _is_gateway_account(account: MessagingAccount | None) -> bool:
    return (
        account is not None
        and account.enabled
        and account.platform == Platform.WHATSAPP
        and account.delivery_mode == DeliveryMode.GATEWAY
    )


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)
