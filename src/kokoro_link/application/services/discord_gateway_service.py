"""Discord Gateway worker for inbound messaging.

Reconnect policy, in two independent halves.

**Parking.** The loop retries forever on transient failures, but stops
for a credential failure that says ``credentials_must_change`` — a
rejected or absent bot token. Retrying those cannot succeed, so it would
only produce an error report every ``reconnect_delay_seconds`` until
someone edits the account. The "stop" is remembered in process memory as
a *fingerprint of the credentials Discord refused*, and the one thing
that lifts it is that fingerprint changing — i.e. someone edited the
credentials. No schema, no extra persisted state, and nothing any worker
writes can move it. A restart or a gateway-lock handover deliberately
costs one more attempt per broken account: confirming the current state
once after a restart is worth more than the single log line it prints.

**Parking is per-process, and that is the whole design.** The memo lives
in this instance's memory, so N connector instances each spend one
attempt on a broken account and then all N go quiet — bounded by the
fleet size, never by uptime. Hosted currently runs a single connector
(the api / coordinator / worker roles are the ones at two instances), so
today that is "one attempt, then silence" — but nothing here depends on
that count, and it must stay that way: the ``connector`` role is defined
as a per-account TTL lease precisely so it *can* be scaled out, and a
rolling deploy briefly runs the old and new connector side by side.

*Why the resume signal is only the fingerprint.* An earlier version also
un-parked when the row's ``updated_at`` moved, reading that as "the
operator re-saved the account, try again". It is not that:
``updated_at`` is the row's last-write time, and
``try_acquire_gateway_lock`` stamps it on every attempt by every
instance (see ``SAMessagingAccountRepository.try_acquire_gateway_lock``,
whose UPDATE sets ``updated_at=now``). With two instances the parks
ping-ponged forever — A parks, B's next lock acquisition stamps the row,
A reads a "repair" and un-parks, stamps it in turn, B un-parks, … — so a
rejected token was back to a reconnect every ``reconnect_delay_seconds``,
which is precisely the storm parking exists to remove. And it was
invisible: the failure reporter de-duplicates identical reports, so the
logs showed one WARNING and nothing else. **Do not add it back.**
Re-saving an account without changing the token changes nothing, so the
retry it would buy is certain to fail the same way; and the credential
failures that really are repaired elsewhere (close 4014, *disallowed
intents*) never park at all — they carry
``credentials_must_change=False`` and keep retrying under the backoff
below. This also puts Discord and WhatsApp on the same rule: see
``WhatsAppGatewayService._known_bad_credentials``.

**Backoff.** A credential failure repaired on Discord's side — close
4014, *disallowed intents* — must keep retrying, because no local edit
would ever un-park it. Reconnecting a doomed handshake every 5 seconds
is not free: each attempt is a ``/gateway/bot`` request plus a WebSocket
connect plus an IDENTIFY, and Discord budgets IDENTIFY per day (1000 for
an unsharded bot), so a fixed 5s retry would spend ~17k of them in a day
and risk the token being rate-limited on top of the original problem.
Consecutive retryable credential failures therefore back off
exponentially from ``reconnect_delay_seconds`` up to
``credential_retry_max_delay_seconds``. The counter lives in memory next
to the parking memo and resets the moment the account connects (or fails
for any other reason), so enabling the intent is noticed within a minute
without a restart.
"""

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

from kokoro_link.application.async_cancellation import reraise_own_cancellation
from kokoro_link.application.services.chat_turn_lease import ConversationBusyError
from kokoro_link.application.services.messaging_dispatcher import MessagingDispatcher
from kokoro_link.application.services.messaging_failure_reporter import (
    MessagingErrorChannel,
    MessagingFailureReport,
    MessagingFailureReporter,
)
from kokoro_link.application.services.messaging_media_owner import (
    MessagingMediaOwnerUnavailable,
    resolve_messaging_media_owner,
)
from kokoro_link.contracts.messaging import (
    InboundMessage,
    MessagingAccountRepositoryPort,
    ParsedInbound,
)
from kokoro_link.contracts.object_storage import ObjectStoragePort
from kokoro_link.contracts.repositories import CharacterRepositoryPort
from kokoro_link.domain.entities.messaging_account import MessagingAccount
from kokoro_link.domain.value_objects.delivery_mode import DeliveryMode
from kokoro_link.domain.value_objects.messaging_failure import (
    MessagingCredentialError,
    MessagingFailureKind,
    credentials_fingerprint,
)
from kokoro_link.domain.value_objects.platform import Platform

DiscordMessageParser = Callable[..., ParsedInbound | None]
DiscordAttachmentDownloader = Callable[..., Awaitable[str | None]]

_LOGGER = logging.getLogger(__name__)
_DUPLICATE_BOT_TOKEN_ERROR = (
    "Duplicate Discord bot token is bound to multiple gateway accounts; "
    "delete duplicate Discord accounts before Gateway can run."
)
_MAX_BACKOFF_DOUBLINGS = 8
"""Enough to reach any sane cap from any sane base (256x), and low
enough that ``2 ** n`` stays a small int no matter how long a broken
account sits there."""


class DiscordGatewayClientPort(Protocol):
    async def connect(
        self,
        *,
        bot_token: str,
        on_message_create: Callable[[dict[str, Any], str | None], Awaitable[None]],
        on_ready: Callable[[], Awaitable[None]] | None = None,
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
        credential_retry_max_delay_seconds: float = 60.0,
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
        self._credential_retry_max_delay_seconds = credential_retry_max_delay_seconds
        self._lock_ttl = timedelta(seconds=lock_ttl_seconds)
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._task: asyncio.Task[None] | None = None
        self._failures = MessagingFailureReporter(
            account_repository=account_repository,
            owner_id=self._owner_id,
            logger=_LOGGER,
            channel=MessagingErrorChannel.GATEWAY,
        )
        # account id -> sha256 fingerprint (never the plaintext
        # credentials — see credentials_fingerprint) of the credentials a
        # CREDENTIAL failure with credentials_must_change=True was
        # reported for. Present = parked. Lifted only when the
        # fingerprint stops matching, i.e. someone edited the account's
        # credentials — deliberately not updated_at, see module docstring.
        # Bounded by _forget_absent_accounts in sync_once; cleared on
        # restart by construction.
        self._parked: dict[str, str] = {}
        # account id -> consecutive credential failures that must keep
        # retrying (Discord close 4014). Drives the backoff only.
        self._credential_retries: dict[str, int] = {}

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
        self._forget_absent_accounts({account.id for account in accounts})
        pollable_accounts, duplicate_accounts = _split_duplicate_bot_tokens(accounts)
        runnable_accounts = [
            account
            for account in pollable_accounts
            if not self._is_parked(account)
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
            await self._mark_duplicate_bot_token_account(account)

        for account in runnable_accounts:
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
            if account is None or not _is_gateway_account(account):
                return
            if self._is_parked(account):
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
            report: MessagingFailureReport | None = None
            try:
                await self._connect_locked_account(locked)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                report = await self._failures.report(
                    locked, exc, context="gateway connect",
                )
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
            if report is not None and not report.should_retry:
                # Credentials the platform rejected and only an edit can fix:
                # leave the loop instead of reconnecting into the same refusal
                # every few seconds. Parked on the credentials we just dialled,
                # so an edit — and nothing else — resumes it.
                self._park_account(locked)
                return
            await asyncio.sleep(self._retry_delay_for(locked.id, report))

    async def _connect_locked_account(self, account: MessagingAccount) -> None:
        token = account.credentials.get("bot_token", "")
        if not token:
            raise MessagingCredentialError(
                "This Discord account has no bot token. Paste the bot token "
                "from the Discord Developer Portal into the account settings.",
                # Pasting the token is exactly a credentials edit, so the
                # fingerprint change un-parks the account without a restart.
                credentials_must_change=True,
            )
        async def handle_ready() -> None:
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
            try:
                inbound = await self._build_inbound(account, parsed)
            except MessagingMediaOwnerUnavailable:
                # A single attachment must not tear down the live Gateway.
                # Ownership is still fail-closed: no download/binding/dispatch.
                _LOGGER.error(
                    "discord inbound media owner unavailable account=%s",
                    account.id,
                )
                await self._record_error(
                    account, "Inbound media owner unavailable",
                )
                return
            try:
                await self._dispatcher.handle_inbound(inbound)
            except ConversationBusyError:
                # The Gateway is a live socket: an exception escaping this
                # handler tears the connection down and reconnects, which does
                # not re-deliver the message and disrupts every other chat on
                # the account. Discord has no re-delivery at all, so the
                # dispatcher's bounded retry was this message's only recovery
                # — log it and keep the socket alive.
                _LOGGER.warning(
                    "discord message dropped — conversation stayed busy "
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

        await self._gateway_client.connect(
            bot_token=token,
            on_message_create=handle_message,
            on_ready=handle_ready,
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
            # Operator-fixable and re-evaluated on every 5s sync, so it goes
            # through the reporter purely for its de-duplication. It is
            # deliberately *not* parked: the fix is deleting the other
            # account, which leaves this account's credentials — and
            # therefore its fingerprint — untouched, so a memo keyed on them
            # would never lift. (The default ``credentials_must_change=False``
            # already says as much; parking here would contradict it.)
            await self._failures.report(
                locked,
                MessagingCredentialError(_DUPLICATE_BOT_TOKEN_ERROR),
                context="duplicate bot token",
            )
        finally:
            await self._accounts.release_gateway_lock(
                locked.id, owner_id=self._owner_id,
            )

    def _is_parked(self, account: MessagingAccount) -> bool:
        """Whether this account's *current* credentials are known-bad.

        Clears the memo as a side effect when the credentials changed: an
        edit is the only signal that makes another attempt worth
        spending, and re-parking costs exactly one attempt if the edit
        was wrong. It is also a signal no worker can forge — nothing on
        the connect / report / lock path writes ``credentials``, so
        another connector instance racing on this row cannot un-park it.
        See the module docstring for why the account's ``updated_at`` is
        deliberately *not* part of this test.
        """
        fingerprint = self._parked.get(account.id)
        if fingerprint is None:
            return False
        if fingerprint == credentials_fingerprint(account.credentials):
            return True
        self._parked.pop(account.id, None)
        return False

    def _forget_absent_accounts(self, live_ids: set[str]) -> None:
        """Keep the in-memory state bounded by the accounts that exist.

        A deleted or disabled account must not pin an entry for the life
        of the process. Re-enabling one costs a single extra attempt,
        which is the same bound a restart already accepts.
        """
        for account_id in list(self._parked):
            if account_id not in live_ids:
                self._parked.pop(account_id, None)
        for account_id in list(self._credential_retries):
            if account_id not in live_ids:
                self._credential_retries.pop(account_id, None)

    def _park_account(self, account: MessagingAccount) -> None:
        """Stop retrying ``account`` until its credentials change.

        Fingerprints the entity the loop was already holding instead of
        re-reading the row. The writes that just happened — the failure
        report's ``polling_last_error``, the lock release — touch no
        credential field, and ``credentials`` only ever moves through an
        operator edit, so the held copy is current for the one field this
        memo reads. (The earlier re-read existed solely to settle
        ``updated_at``, which those writes really did move; that half of
        the memo is gone.)

        Re-reading would in fact be *wrong* under a concurrent edit: it
        would fingerprint the freshly pasted token as the bad one and
        park a credential that was never dialled. The held, pre-edit
        fingerprint instead stops matching the row and un-parks on the
        next sync — exactly what the edit asked for.
        """
        self._credential_retries.pop(account.id, None)
        self._parked[account.id] = credentials_fingerprint(account.credentials)

    def _retry_delay_for(
        self,
        account_id: str,
        report: MessagingFailureReport | None,
    ) -> float:
        """How long to wait before dialling this account again.

        A credential failure that must keep retrying (Discord close
        4014) is a handshake that is certain to fail, and each attempt
        spends an IDENTIFY out of a daily budget — so consecutive ones
        back off exponentially. Everything else, success included, resets
        the counter and uses the plain reconnect delay, which keeps the
        recovery latency at one delay once the operator ticks the intent.
        """
        if report is None or report.kind is not MessagingFailureKind.CREDENTIAL:
            self._credential_retries.pop(account_id, None)
            return self._reconnect_delay_seconds
        attempts = self._credential_retries.get(account_id, 0) + 1
        self._credential_retries[account_id] = attempts
        # Capped exponent as well as capped delay: the multiplication must
        # not grow unbounded just because the cap would clamp it anyway.
        growth = 2 ** min(attempts - 1, _MAX_BACKOFF_DOUBLINGS)
        return min(
            self._reconnect_delay_seconds * growth,
            self._credential_retry_max_delay_seconds,
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
        return await resolve_messaging_media_owner(
            characters=self._characters,
            account=account,
            logger=_LOGGER,
        )

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
