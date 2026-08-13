"""Single entry point for reporting a messaging worker failure.

Telegram polling, the Discord gateway and the WhatsApp gateway all hit
the same four steps when an account fails: classify the failure, decide
how loudly to log it, avoid repeating an identical line forever, and
persist the message onto the account so the operator can see it in the
UI. Each service used to open-code that (and each got it slightly
wrong — a wrong bot token produced an ERROR with a full traceback every
5 seconds, forever). :class:`MessagingFailureReporter` owns the whole
sequence so the services only have to say *what* went wrong.

Two logging destinations, chosen by failure kind — not by deployment:

* ``CREDENTIAL`` → the dedicated logger
  ``kokoro_link.messaging.account_config`` at ``WARNING``, **without a
  traceback**. A dedicated logger name is the deployment seam: hosted
  ops can silence or reroute exactly this stream through ordinary
  logging config, while a self-hoster reads a one-line, human-readable
  cause. No ``if hosted:`` branch exists anywhere in the code path.
* ``TRANSIENT`` → the caller's own module logger at ``ERROR`` with the
  traceback attached, i.e. the previous behaviour, because ops genuinely
  needs it.

Both carry structured ``extra`` fields (``account_id``,
``character_id``, ``platform``, ``failure_kind``) so multi-tenant alert
rules can drop a whole kind without losing queryability.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum

from kokoro_link.contracts.messaging import MessagingAccountRepositoryPort
from kokoro_link.domain.entities.messaging_account import MessagingAccount
from kokoro_link.domain.value_objects.messaging_failure import (
    MessagingCredentialError,
    MessagingFailureKind,
)

CREDENTIAL_LOGGER_NAME = "kokoro_link.messaging.account_config"
"""Dedicated logger for operator-fixable credential failures.

Public so deployments (and tests) can reference the exact name instead
of copying the string."""

_CREDENTIAL_LOGGER = logging.getLogger(CREDENTIAL_LOGGER_NAME)

MAX_ERROR_LENGTH = 1000
"""The single source of truth for how long a stored messaging error may
be. Truncating *here*, before de-duplication compares against the
stored value, keeps the comparison consistent with what actually ends
up in ``polling_last_error`` — that's the only reason this constant
exists rather than leaving truncation to each repository.

Both :class:`~kokoro_link.infrastructure.persistence.
sa_messaging_account_repository.SAMessagingAccountRepository` and
:class:`~kokoro_link.infrastructure.repositories.
in_memory_messaging_accounts.InMemoryMessagingAccountRepository`
import this constant and truncate ``record_polling_error`` writes to
it — they do not hardcode their own length. If a repository ever
truncates to a *different* number than this one, de-duplication goes
quietly wrong only for callers whose message exceeds both lengths
(the reporter compares against its own already-truncated copy, the
repository stores a copy truncated to its own number, and the two
stop agreeing): keep any future change to this constant paired with
the repositories' imports, never with a second literal."""


class MessagingErrorChannel(StrEnum):
    """Which repository recorder a worker persists its error through."""

    GATEWAY = "gateway"
    POLLING = "polling"


@dataclass(frozen=True, slots=True)
class MessagingFailureReport:
    """What the reporter decided, for the caller and for assertions."""

    kind: MessagingFailureKind
    message: str
    """The (truncated) text persisted to ``polling_last_error``."""
    should_retry: bool
    """Whether the worker should keep trying.

    **Not derived from :attr:`kind`.** Only a credential failure that
    declared ``credentials_must_change`` yields ``False``, because only
    then does a local signal (the credentials fingerprint changing)
    exist to resume from. A credential failure repaired on the
    platform's side — Discord close 4014, *disallowed intents* — stays
    ``True``: nothing locally observable would ever un-park it."""
    logged: bool
    """``False`` when suppressed as a repeat of the stored error."""
    recorded: bool
    """Whether ``record_*_error`` actually wrote (it returns ``False``
    when the caller no longer holds the account lock)."""


class MessagingFailureReporter:
    """Classify → log → de-duplicate → persist, for one worker.

    One instance per service (the logger and the error channel are
    fixed per worker); ``owner_id`` is the worker's lock owner id,
    since the repository only accepts an error write from the current
    lock holder.

    **De-duplication reads its own baseline.** The reporter re-reads the
    account's stored ``polling_last_error`` from the repository rather
    than trusting the entity it was handed. Callers acquire an account
    once per round and then hold it across a long-lived connection, so
    the in-memory entity is a frozen snapshot: a successful connect
    clears the stored error via ``mark_*_success`` while that snapshot
    keeps the old text, and "fail → succeed → fail again" would be
    suppressed as a repeat of a record that no longer exists. Requiring
    callers to re-read instead was tried; all three of them got it
    wrong, which makes it the wrong side of the interface. The extra
    read costs one query per *failure* — the success path never calls
    ``report`` at all.
    """

    def __init__(
        self,
        *,
        account_repository: MessagingAccountRepositoryPort,
        owner_id: str,
        logger: logging.Logger,
        channel: MessagingErrorChannel,
    ) -> None:
        self._accounts = account_repository
        self._owner_id = owner_id
        self._logger = logger
        self._channel = channel

    async def report(
        self,
        account: MessagingAccount,
        failure: BaseException | str,
        *,
        at: datetime | None = None,
        kind: MessagingFailureKind | None = None,
        context: str | None = None,
    ) -> MessagingFailureReport:
        """Report one failure for ``account``.

        ``failure`` is either the caught exception or a ready-made
        message. Classification: a :class:`MessagingCredentialError`
        (or subclass) is CREDENTIAL, anything else is TRANSIENT — unless
        the caller passes ``kind`` explicitly, which it must do when the
        protocol signal arrives as data rather than as an exception
        (Telegram reports auth failure as an ``error_code`` in a JSON
        body, so its polling loop classifies at the parse site).

        ``context`` is an optional short label for *where* the failure
        happened ("gateway connect", "update 42 processing"); it is
        logged but deliberately excluded from the persisted message and
        from the de-duplication key, so a rotating context cannot defeat
        suppression.

        The returned ``should_retry`` comes from the failure itself, not
        from ``kind``: only a :class:`MessagingCredentialError` raised
        with ``credentials_must_change=True`` answers ``False``. Passing
        ``kind=CREDENTIAL`` with a bare string therefore keeps retrying
        — a caller that needs the account parked must raise the typed
        error so the recovery signal is stated at the raise site.
        """
        resolved_kind = kind or _classify(failure)
        message = self._message_of(failure)
        logged = message != await self._stored_error(account.id)
        if logged:
            self._log(account, resolved_kind, message, failure, context)
        recorded = await self._record(account, message, at or _utcnow())
        return MessagingFailureReport(
            kind=resolved_kind,
            message=message,
            should_retry=_should_retry(resolved_kind, failure),
            logged=logged,
            recorded=recorded,
        )

    async def _stored_error(self, account_id: str) -> str | None:
        """The error text currently persisted for ``account_id``.

        ``None`` when the account has no stored error *or* no longer
        exists — a deletion mid-flight must not raise into the caller's
        worker loop, and treating it as "nothing stored" errs towards
        logging one extra line rather than swallowing a new failure.
        """
        stored = await self._accounts.get(account_id)
        if stored is None:
            return None
        return stored.polling_last_error

    def _message_of(self, failure: BaseException | str) -> str:
        if isinstance(failure, MessagingCredentialError):
            raw = failure.user_message
        elif isinstance(failure, BaseException):
            raw = str(failure) or failure.__class__.__name__
        else:
            raw = failure
        return raw[:MAX_ERROR_LENGTH]

    def _log(
        self,
        account: MessagingAccount,
        kind: MessagingFailureKind,
        message: str,
        failure: BaseException | str,
        context: str | None,
    ) -> None:
        extra = {
            "account_id": account.id,
            "character_id": account.character_id,
            "platform": account.platform.value,
            "failure_kind": kind.value,
        }
        where = context or self._channel.value
        if kind is MessagingFailureKind.CREDENTIAL:
            # No traceback: the stack tells the operator nothing they can
            # act on, and this line repeats for every reconnect attempt.
            _CREDENTIAL_LOGGER.warning(
                "%s account credentials rejected account=%s where=%s: %s",
                account.platform.value,
                account.id,
                where,
                message,
                extra=extra,
            )
            return
        # Transient: keep the caller's logger and the traceback. Passing
        # the exception object explicitly (instead of exc_info=True) keeps
        # this correct when the caller reports outside an except block —
        # a data-derived TRANSIENT message then logs without a bogus
        # "NoneType: None" tail.
        self._logger.error(
            "%s account failure account=%s where=%s: %s",
            account.platform.value,
            account.id,
            where,
            message,
            exc_info=failure if isinstance(failure, BaseException) else None,
            extra=extra,
        )

    async def _record(
        self,
        account: MessagingAccount,
        message: str,
        at: datetime,
    ) -> bool:
        if self._channel is MessagingErrorChannel.GATEWAY:
            return await self._accounts.record_gateway_error(
                account.id,
                owner_id=self._owner_id,
                error=message,
                at=at,
            )
        return await self._accounts.record_polling_error(
            account.id,
            owner_id=self._owner_id,
            error=message,
            at=at,
        )


def _classify(failure: BaseException | str) -> MessagingFailureKind:
    if isinstance(failure, MessagingCredentialError):
        return MessagingFailureKind.CREDENTIAL
    return MessagingFailureKind.TRANSIENT


def _should_retry(
    kind: MessagingFailureKind,
    failure: BaseException | str,
) -> bool:
    """Whether the worker has a reason to try this account again.

    Independent of ``kind``: only the raise site knows whether the
    repair will show up locally as a credentials edit. A caller that
    declares ``kind=CREDENTIAL`` for a non-exception signal (Telegram's
    JSON ``error_code``) cannot express that, so it gets the safe answer
    — keep retrying — rather than an unrecoverable park.
    """
    if kind is not MessagingFailureKind.CREDENTIAL:
        return True
    if isinstance(failure, MessagingCredentialError):
        return not failure.credentials_must_change
    return True


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)
