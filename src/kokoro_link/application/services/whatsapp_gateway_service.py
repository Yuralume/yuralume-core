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

import httpx

from kokoro_link.application.async_cancellation import reraise_own_cancellation
from kokoro_link.application.services.chat_turn_lease import ConversationBusyError
from kokoro_link.application.services.messaging_dispatcher import MessagingDispatcher
from kokoro_link.application.services.messaging_failure_reporter import (
    MessagingErrorChannel,
    MessagingFailureReporter,
)
from kokoro_link.contracts.messaging import (
    InboundMessage,
    MessagingAccountRepositoryPort,
    ParsedInbound,
)
from kokoro_link.domain.entities.messaging_account import MessagingAccount
from kokoro_link.domain.value_objects.delivery_mode import DeliveryMode
from kokoro_link.domain.value_objects.messaging_failure import (
    MessagingCredentialError,
    MessagingFailureKind,
    credentials_fingerprint,
)
from kokoro_link.domain.value_objects.platform import Platform

WhatsAppEventParser = Callable[[dict[str, Any]], ParsedInbound | None]

_LOGGER = logging.getLogger(__name__)
_DUPLICATE_SESSION_ERROR = (
    "Duplicate WhatsApp sidecar session is bound to multiple gateway accounts; "
    "delete duplicate WhatsApp accounts before Gateway can run."
)
# The sidecar is a local Baileys-compatible HTTP/SSE service, not a cloud
# platform: only an explicit auth rejection from *it* (wrong api_token) is a
# CREDENTIAL failure. A sidecar that is merely unreachable (connection
# refused, DNS, timeout) is very likely "the user hasn't started it yet" —
# that stays TRANSIENT and keeps retrying; the failure reporter's
# de-duplication (not a retry-stop) is what keeps it from spamming a
# traceback every reconnect_delay_seconds.
_CREDENTIAL_STATUS_CODES = frozenset({401, 403})


class WhatsAppSidecarClientPort(Protocol):
    async def connect(
        self,
        *,
        sidecar_url: str,
        session_id: str,
        api_token: str | None,
        on_event: Callable[[dict[str, Any]], Awaitable[None]],
        on_connected: Callable[[], Awaitable[None]] | None = None,
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
        self._failures = MessagingFailureReporter(
            account_repository=account_repository,
            owner_id=self._owner_id,
            logger=_LOGGER,
            channel=MessagingErrorChannel.GATEWAY,
        )
        # account id -> sha256 fingerprint (never the plaintext credentials
        # — see credentials_fingerprint) of the last account.credentials a
        # CREDENTIAL failure with credentials_must_change=True was reported
        # for. Recovery condition: the fingerprint no longer matches (the
        # operator edited the account). Bounded by _forget_absent_accounts
        # in sync_once; cleared on restart by construction.
        self._known_bad_credentials: dict[str, str] = {}

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
        self._forget_absent_accounts({account.id for account in accounts})
        pollable_accounts, duplicate_accounts = _split_duplicate_sessions(accounts)
        runnable_accounts = [
            account
            for account in pollable_accounts
            if not self._is_known_bad(account)
        ]
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
            if account is None or not _is_gateway_account(account):
                return
            if self._is_known_bad(account):
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
            retry = True
            try:
                await self._connect_locked_account(locked)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failure = _as_reportable_failure(exc)
                report = await self._failures.report(
                    locked,
                    failure,
                    context="gateway connect",
                )
                retry = report.should_retry
                if not retry:
                    self._mark_known_bad(locked)
            finally:
                refresher.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await refresher
                await self._accounts.release_gateway_lock(
                    locked.id,
                    owner_id=self._owner_id,
                )
                # Only after the lock is back: the suppress above can eat
                # this task's *own* cancellation, and dropping out here
                # without releasing would strand the account until the TTL.
                reraise_own_cancellation(current_task)
            if not retry:
                # Credentials the sidecar rejected: leave the loop instead
                # of reconnecting into the same refusal every few seconds.
                # The lock is already released above, so another worker (or
                # this one after an edit) can pick the account up. Mirrors
                # the Discord gateway's shape: the task ends and drops out
                # of tasks_running on the next sync tick instead of staying
                # alive to poll the DB every reconnect_delay_seconds.
                return
            await asyncio.sleep(self._reconnect_delay_seconds)

    def _is_known_bad(self, account: MessagingAccount) -> bool:
        """Whether this account's *current* credentials are known-bad.

        Clears the memo as a side effect when the credentials changed: an
        edit is the only signal that makes a retry worth spending.
        """
        fingerprint = self._known_bad_credentials.get(account.id)
        if fingerprint is None:
            return False
        if fingerprint == credentials_fingerprint(account.credentials):
            return True
        self._known_bad_credentials.pop(account.id, None)
        return False

    def _mark_known_bad(self, account: MessagingAccount) -> None:
        self._known_bad_credentials[account.id] = credentials_fingerprint(
            account.credentials,
        )

    def _forget_absent_accounts(self, live_ids: set[str]) -> None:
        """Keep the memo bounded by the accounts that still exist.

        A deleted or disabled account must not pin an entry — and its
        plaintext-free fingerprint — for the life of the process.
        Re-enabling one costs a single extra attempt, which is the same
        bound a restart already accepts.
        """
        for account_id in list(self._known_bad_credentials):
            if account_id not in live_ids:
                self._known_bad_credentials.pop(account_id, None)

    async def _connect_locked_account(self, account: MessagingAccount) -> None:
        sidecar_url = account.credentials.get("sidecar_url", "").strip().rstrip("/")
        session_id = account.credentials.get("session_id", "").strip()
        api_token = account.credentials.get("api_token", "").strip() or None
        if not sidecar_url or not session_id:
            # Nothing to dial yet — this is a config gap only the operator
            # can close, not something a reconnect attempt can fix.
            raise MessagingCredentialError(
                "WhatsApp account is missing sidecar_url/session_id. "
                "Fill in both fields in the account settings.",
                # Filling in the fields is exactly a credentials edit, so
                # the fingerprint change un-parks the account without a
                # restart — the same reasoning as Discord's missing
                # bot_token raise.
                credentials_must_change=True,
            )

        async def handle_connected() -> None:
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
            try:
                await self._dispatcher.handle_inbound(inbound)
            except ConversationBusyError:
                # Same reasoning as the Discord gateway: this handler runs on a
                # live sidecar socket that has no re-delivery, so an escaping
                # exception costs the connection without saving the message.
                _LOGGER.warning(
                    "whatsapp message dropped — conversation stayed busy "
                    "account=%s chat=%s id=%s",
                    account.id,
                    inbound.chat_ref,
                    inbound.platform_message_id,
                )
                return
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
            on_connected=handle_connected,
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
            # Two accounts pointed at the same sidecar session is a local
            # configuration conflict, not a platform credential rejection —
            # but it is equally unfixable by retrying, so it shares the
            # CREDENTIAL treatment (quiet WARNING, de-duplicated) rather
            # than paging ops with a TRANSIENT-style traceback every sync
            # tick.
            await self._failures.report(
                locked,
                _DUPLICATE_SESSION_ERROR,
                kind=MessagingFailureKind.CREDENTIAL,
                context="duplicate session",
            )
        finally:
            await self._accounts.release_gateway_lock(
                locked.id,
                owner_id=self._owner_id,
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


def _as_reportable_failure(exc: Exception) -> BaseException:
    """Fold a caught exception into the reporter's vocabulary.

    ``WhatsAppSidecarClientPort`` is a plain ``Protocol`` — it carries no
    domain-level failure type, so the one concrete adapter
    (``WhatsAppSidecarClient``) surfaces the sidecar's HTTP response as a
    bare ``httpx.HTTPStatusError``. Reading its structured
    ``response.status_code`` here (never its message text) is the
    protocol-level signal the LLM-first / no-string-special-casing rule
    asks for; only 401/403 — the sidecar explicitly rejecting the
    ``api_token`` — becomes a :class:`MessagingCredentialError`. Every
    other failure (connection refused, DNS, timeout, other status codes)
    passes through unchanged and stays TRANSIENT.
    """
    if isinstance(exc, MessagingCredentialError):
        return exc
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status in _CREDENTIAL_STATUS_CODES:
            return MessagingCredentialError(
                f"WhatsApp sidecar rejected the api_token (HTTP {status}). "
                "Update the token in the account settings.",
                # The sidecar refused this exact token, so the only route
                # back is editing it — which is a locally observable
                # signal (the credentials fingerprint changes).
                credentials_must_change=True,
            )
    return exc
