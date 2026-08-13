"""Telegram long-polling worker for inbound messaging.

Retry policy: every sweep re-polls every eligible account, *except* those
whose credentials Telegram itself rejected. Re-sending ``getUpdates`` with
a token the API answered 401 to cannot start working — at a 2s cadence
that is ~43k pointless calls per broken account per day, and one stored
error rewritten just as often. The "stop" is remembered as a
:func:`~kokoro_link.domain.value_objects.messaging_failure.credentials_fingerprint`
in process memory, so editing the token resumes the account on the very
next sweep with no schema and no extra persisted state. A restart or a
lock handover deliberately costs one more attempt per broken account:
re-confirming the current state once is worth more than the single
de-duplicated log line it prints.

Only a failure whose *only* repair is editing the stored credentials may
park an account, because only then does the fingerprint give a local
signal to un-park it — see ``MessagingCredentialError``. A duplicate bot
token is fixed by deleting the *other* account, which leaves this one's
credentials byte-identical, so it is reported every sweep and never
parked.
"""

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

from kokoro_link.application.services.chat_turn_lease import ConversationBusyError
from kokoro_link.application.services.messaging_dispatcher import MessagingDispatcher
from kokoro_link.application.services.messaging_failure_reporter import (
    MessagingErrorChannel,
    MessagingFailureReporter,
)
from kokoro_link.application.services.messaging_media_owner import (
    resolve_messaging_media_owner,
)
from kokoro_link.contracts.messaging import (
    InboundMessage,
    MessagingAccountRepositoryPort,
    ParsedInbound,
    TelegramPollingPort,
)
from kokoro_link.contracts.object_storage import ObjectStoragePort
from kokoro_link.contracts.repositories import CharacterRepositoryPort
from kokoro_link.domain.entities.messaging_account import MessagingAccount
from kokoro_link.domain.value_objects.messaging_failure import (
    MessagingCredentialError,
    MessagingFailureKind,
    credentials_fingerprint,
)

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
MISSING_BOT_TOKEN_ERROR = (
    "This Telegram account has no bot token. Open a chat with @BotFather, "
    "send /mybots to copy the token for your bot, and paste it into this "
    "account's settings."
)
"""Public: the webhook routes reuse this so both surfaces say the same thing."""
_CREDENTIAL_ERROR_MESSAGES: dict[int, str] = {
    401: (
        "Telegram rejected the bot token (HTTP 401). Check this account's "
        "Telegram bot token — asking @BotFather to revoke or regenerate it "
        "invalidates the old one, so the account needs the new token pasted "
        "in."
    ),
    403: (
        "Telegram refused this bot's polling request (HTTP 403). The token "
        "is no longer usable — ask @BotFather for the bot's current token "
        "(or issue a fresh one with /revoke) and paste it into this "
        "account's settings."
    ),
}
"""Telegram ``error_code`` values that mean the platform rejected the
bot's credentials outright (Unauthorized / Forbidden), mapped to what the
*account owner* should do about it.

Read the structured field only; never match on ``description`` text. The
description is Telegram's own wording aimed at API developers — a bare
"Unauthorized" persisted into ``polling_last_error`` and rendered in the
account panel tells a self-hosting player nothing they can act on.

Both codes carry ``credentials_must_change=True``: unlike Discord's close
code 4014 (*disallowed intents*, repaired by ticking a checkbox in a
portal), neither of these can be repaired without the account storing a
different token, so the fingerprint change is a real un-park signal."""


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
        self._failures = MessagingFailureReporter(
            account_repository=account_repository,
            owner_id=self._owner_id,
            logger=_LOGGER,
            channel=MessagingErrorChannel.POLLING,
        )
        # account id -> fingerprint of the credentials Telegram rejected.
        # Holds no secret material; see ``credentials_fingerprint``.
        self._rejected_credentials: dict[str, str] = {}

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
        self._forget_absent_accounts({account.id for account in accounts})
        if not accounts:
            return TelegramPollingSweepResult(
                accounts_seen=0, acquired=0, updates_seen=0, dispatched=0,
            )

        # Duplicates are split from the *full* candidate list, before the
        # parked ones are dropped: hiding a member of a duplicate group
        # would make the survivor look unique and start polling a token
        # that is still shared.
        pollable_accounts, duplicate_accounts = _split_duplicate_bot_tokens(
            accounts,
        )
        runnable_accounts = [
            account
            for account in pollable_accounts
            if not self._credentials_rejected(account)
        ]

        semaphore = asyncio.Semaphore(self._max_concurrency)
        results = await asyncio.gather(
            *(
                self._mark_duplicate_bot_token_account(account, semaphore)
                for account in duplicate_accounts
            ),
            *(self._poll_account(account, semaphore) for account in runnable_accounts),
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
                # Operator-fixable and re-evaluated every sweep, so it goes
                # through the reporter purely for its de-duplication — that
                # single account-scoped WARNING is the whole log budget for
                # this condition. Deliberately built *without*
                # ``credentials_must_change``: the fix is deleting the other
                # account, which leaves this one's credentials — and so its
                # fingerprint — untouched, so a park keyed on them would
                # never lift.
                error = await self._report(
                    locked,
                    MessagingCredentialError(_DUPLICATE_BOT_TOKEN_ERROR),
                    at=now,
                    context="duplicate bot token",
                )
                return TelegramPollingAccountResult(
                    account_id=account.id,
                    acquired=True,
                    error=error,
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
            error = await self._report(
                account,
                MessagingCredentialError(
                    MISSING_BOT_TOKEN_ERROR,
                    # Pasting the token *is* a credentials edit, so the
                    # fingerprint change un-parks this without a restart.
                    credentials_must_change=True,
                ),
                context="missing bot_token",
            )
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
            error = await self._report(
                account,
                _telegram_failure(response),
                at=checked_at,
                context="getUpdates",
            )
            return TelegramPollingAccountResult(
                account_id=account.id, acquired=True, error=error,
            )

        raw_updates = response.get("result")
        if not isinstance(raw_updates, list):
            # Transient: a malformed body is Telegram (or a proxy) misbehaving,
            # not a verdict on this account's token — polling must keep going.
            error = await self._report(
                account,
                "Telegram getUpdates returned an unexpected result shape",
                at=checked_at,
                kind=MessagingFailureKind.TRANSIENT,
                context="getUpdates result shape",
            )
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
            except ConversationBusyError:
                # Stop the sweep *without* advancing the offset: Telegram hands
                # the same update back on the next poll (~2s), which is exactly
                # the re-delivery the dispatcher rolled its dedup stamps back
                # for. Deliberately not recorded as an account error — nothing
                # is wrong with the bot, one conversation is simply mid-turn.
                _LOGGER.info(
                    "telegram polling deferred busy conversation "
                    "account=%s update_id=%s",
                    account.id,
                    update_id,
                )
                return TelegramPollingAccountResult(
                    account_id=account.id,
                    acquired=True,
                    updates_seen=updates_seen,
                    dispatched=dispatched,
                )
            except Exception as exc:
                # Transient: one update blew up, the credentials are fine —
                # the next sweep re-fetches it (the offset never advanced).
                # Report the caught exception itself (not a synthesized
                # string) so the reporter's own ERROR line carries the
                # traceback — it logs with ``exc_info=failure`` for
                # TRANSIENT kinds, which is the same information a
                # dedicated ``_LOGGER.exception`` call here used to
                # duplicate. That second line was pure noise: this exact
                # failure is retried and reported again on the very next
                # sweep anyway (the offset never advances), so nothing
                # about which update failed is lost by dropping it — see
                # ``context`` below for that.
                # The reporter's de-duplication never engages on this path,
                # and that is correct, not a leftover bug: `mark_polling_success`
                # just cleared `polling_last_error` to ``None`` a few lines up
                # (this sweep's ``getUpdates`` call *did* succeed), so every
                # sweep genuinely starts from "no stored error" before this
                # same broken update fails again. Each of those ERROR lines
                # reports a real, currently-happening failure — a stuck
                # update has no local un-park signal the way a rejected
                # credential does (see the module docstring), so this is the
                # only observability an operator gets that something needs
                # manual intervention. Suppressing it would silently swallow
                # an ongoing failure, not just quiet a stale repeat.
                error = await self._report(
                    account,
                    exc,
                    kind=MessagingFailureKind.TRANSIENT,
                    context=f"update {update_id} processing",
                )
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

    async def _report(
        self,
        account: MessagingAccount,
        failure: BaseException | str,
        *,
        at: datetime | None = None,
        kind: MessagingFailureKind | None = None,
        context: str,
    ) -> str:
        """Report one account failure and act on the retry verdict.

        Every ``report`` call in this service goes through here, because
        dropping the returned verdict is exactly how the polling loop
        kept hammering a rejected token: the reporter made the failure
        quiet, not cheap. Returns the persisted (truncated) message so
        the sweep result carries the same text the account panel shows.
        """
        report = await self._failures.report(
            account, failure, at=at, kind=kind, context=context,
        )
        if not report.should_retry:
            self._remember_rejected_credentials(account)
        return report.message

    def _credentials_rejected(self, account: MessagingAccount) -> bool:
        """Whether this account's *current* credentials are known-bad.

        Clears the memo as a side effect when the credentials changed:
        an edit is the only signal that makes a retry worth spending.
        """
        rejected = self._rejected_credentials.get(account.id)
        if rejected is None:
            return False
        if rejected == credentials_fingerprint(account.credentials):
            return True
        self._rejected_credentials.pop(account.id, None)
        return False

    def _forget_absent_accounts(self, live_ids: set[str]) -> None:
        """Keep the memo bounded by the accounts that still exist.

        A deleted, disabled or webhook-switched account must not pin an
        entry for the life of the process. Bringing one back costs a
        single extra attempt, the same bound a restart already accepts.
        """
        for account_id in list(self._rejected_credentials):
            if account_id not in live_ids:
                self._rejected_credentials.pop(account_id, None)

    def _remember_rejected_credentials(self, account: MessagingAccount) -> None:
        self._rejected_credentials[account.id] = credentials_fingerprint(
            account.credentials,
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
        return await resolve_messaging_media_owner(
            characters=self._characters,
            account=account,
            logger=_LOGGER,
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


def _telegram_failure(
    response: dict[str, Any],
) -> MessagingCredentialError | str:
    """Turn a failed ``getUpdates`` body into something reportable.

    Telegram signals an unrecoverable bad token as data — ``{"ok": false,
    "error_code": 401, ...}`` — rather than raising, so the polling loop
    reads the numeric field itself and hands the reporter the same typed
    error an adapter would have raised. Matching on ``description`` text
    is forbidden by the project's no-string-special-casing rule; the code
    is the protocol-level signal, the description is free-form prose
    Telegram is free to reword.

    Returning the typed error rather than a ``kind=`` hint is what lets
    the sweep stop: only a :class:`MessagingCredentialError` can state
    ``credentials_must_change``, and a bare string tagged CREDENTIAL is
    deliberately given the safe answer (keep retrying) by the reporter.
    Anything unrecognised stays transient and keeps Telegram's own
    description, which is the text ops wants in the ERROR line.
    """
    code = response.get("error_code")
    message = (
        _CREDENTIAL_ERROR_MESSAGES.get(code) if isinstance(code, int) else None
    )
    if message is not None:
        return MessagingCredentialError(message, credentials_must_change=True)
    return _telegram_error(response)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)
