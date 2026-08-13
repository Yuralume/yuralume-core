"""MessagingFailureReporter — classify, log, de-duplicate, persist.

Pins the operational contract the three messaging workers depend on:
credential failures go to the dedicated ``kokoro_link.messaging.
account_config`` logger at WARNING without a traceback, transient
failures keep the caller's logger and the traceback, and an unchanged
error stops re-logging while still refreshing the stored record.

Two structural properties get their own pins here, because both were
regressions the workers could not see:

* de-duplication compares against the **stored** error, which the
  reporter reads itself — the callers hold a frozen entity snapshot
  across a whole connection, so anything derived from that snapshot
  goes stale the moment a success clears the record;
* "keep retrying" is **not** a function of the failure kind — a
  credential failure repaired on the platform's side must keep
  retrying, since no local signal would ever un-park it.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.application.services.messaging_failure_reporter import (
    CREDENTIAL_LOGGER_NAME,
    MessagingErrorChannel,
    MessagingFailureReporter,
)
from kokoro_link.domain.entities.messaging_account import MessagingAccount
from kokoro_link.domain.value_objects.messaging_failure import (
    MessagingCredentialError,
    MessagingFailureKind,
)
from kokoro_link.domain.value_objects.platform import Platform
from kokoro_link.infrastructure.repositories.in_memory_messaging_accounts import (
    InMemoryMessagingAccountRepository,
)

pytestmark = pytest.mark.asyncio

_OWNER = "discord-gateway-test"
_CALLER_LOGGER = logging.getLogger("tests.messaging_failure_reporter.caller")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def _locked_account(
    repo: InMemoryMessagingAccountRepository,
    *,
    platform: Platform = Platform.DISCORD,
) -> MessagingAccount:
    account = MessagingAccount.create(
        character_id="char-1",
        platform=platform,
        credentials={"bot_token": "wrong-token"},
        display_name="Test bot",
    )
    await repo.save(account)
    locked = await repo.try_acquire_gateway_lock(
        account.id,
        owner_id=_OWNER,
        now=_utcnow(),
        ttl=timedelta(seconds=90),
    )
    assert locked is not None
    return locked


def _reporter(
    repo: InMemoryMessagingAccountRepository,
    *,
    channel: MessagingErrorChannel = MessagingErrorChannel.GATEWAY,
) -> MessagingFailureReporter:
    return MessagingFailureReporter(
        account_repository=repo,
        owner_id=_OWNER,
        logger=_CALLER_LOGGER,
        channel=channel,
    )


async def test_credential_failure_warns_without_traceback(caplog) -> None:
    repo = InMemoryMessagingAccountRepository()
    account = await _locked_account(repo)
    reporter = _reporter(repo)

    with caplog.at_level(logging.DEBUG):
        report = await reporter.report(
            account,
            MessagingCredentialError(
                "Discord rejected the bot token (HTTP 401)",
                credentials_must_change=True,
            ),
        )

    assert report.kind is MessagingFailureKind.CREDENTIAL
    assert report.should_retry is False
    assert report.logged is True
    assert report.recorded is True

    records = [r for r in caplog.records if r.name == CREDENTIAL_LOGGER_NAME]
    assert len(records) == 1
    record = records[0]
    assert record.levelno == logging.WARNING
    assert record.exc_info is None
    assert "Discord rejected the bot token (HTTP 401)" in record.getMessage()
    # The caller's own logger must stay silent for credential failures.
    assert [r for r in caplog.records if r.name == _CALLER_LOGGER.name] == []

    stored = await repo.get(account.id)
    assert stored is not None
    assert stored.polling_last_error == (
        "Discord rejected the bot token (HTTP 401)"
    )


async def test_transient_failure_logs_exception_on_caller_logger(caplog) -> None:
    repo = InMemoryMessagingAccountRepository()
    account = await _locked_account(repo)
    reporter = _reporter(repo)

    try:
        raise RuntimeError("gateway socket closed")
    except RuntimeError as exc:
        with caplog.at_level(logging.DEBUG):
            report = await reporter.report(account, exc)

    assert report.kind is MessagingFailureKind.TRANSIENT
    assert report.should_retry is True
    assert report.logged is True

    records = [r for r in caplog.records if r.name == _CALLER_LOGGER.name]
    assert len(records) == 1
    record = records[0]
    assert record.levelno == logging.ERROR
    assert record.exc_info is not None
    assert "gateway socket closed" in record.getMessage()
    assert [r for r in caplog.records if r.name == CREDENTIAL_LOGGER_NAME] == []


async def test_repeated_identical_error_is_recorded_but_not_logged(caplog) -> None:
    repo = InMemoryMessagingAccountRepository()
    account = await _locked_account(repo)
    reporter = _reporter(repo)
    failure = MessagingCredentialError("Discord rejected the bot token (HTTP 401)")

    with caplog.at_level(logging.DEBUG):
        first = await reporter.report(account, failure, at=_utcnow())
        caplog.clear()
        # Same entity object both times — a worker holds one snapshot for
        # the life of the connection. Suppression must come from what is
        # stored, not from the snapshot.
        later = _utcnow()
        second = await reporter.report(account, failure, at=later)

    assert first.logged is True
    assert second.logged is False
    assert second.recorded is True
    assert caplog.records == []

    stored = await repo.get(account.id)
    assert stored is not None
    assert stored.polling_last_error == first.message
    assert stored.polling_last_update_at == later


async def test_changed_error_message_logs_again(caplog) -> None:
    repo = InMemoryMessagingAccountRepository()
    account = await _locked_account(repo)
    reporter = _reporter(repo)

    with caplog.at_level(logging.DEBUG):
        await reporter.report(
            account, MessagingCredentialError("Discord rejected the bot token"),
        )
        caplog.clear()
        second = await reporter.report(
            account,
            MessagingCredentialError("Discord disallowed the requested intents"),
        )

    assert second.logged is True
    records = [r for r in caplog.records if r.name == CREDENTIAL_LOGGER_NAME]
    assert len(records) == 1
    assert "disallowed the requested intents" in records[0].getMessage()


async def test_success_clears_record_so_next_failure_logs_again(caplog) -> None:
    """fail → success → same failure must log a second time.

    The regression this pins: the worker acquires the account once, then
    ``mark_*_success`` clears ``polling_last_error`` in the repository
    while the worker's entity keeps the stale text. Comparing against
    that snapshot suppressed every reconnect after the first — a drop /
    reconnect cycle went completely silent. **No re-read here on
    purpose**: the entity handed to the reporter is deliberately stale,
    exactly as all three real callers hand it over.
    """
    repo = InMemoryMessagingAccountRepository()
    account = await _locked_account(repo)
    reporter = _reporter(repo)
    failure = MessagingCredentialError("Discord rejected the bot token")

    # Round 1 fails and stores the error.
    first = await reporter.report(account, failure)
    assert first.logged is True

    # Round 2 starts: the worker acquires the account, so its snapshot
    # now carries round 1's error text. It connects successfully, which
    # clears the stored error — and then the connection drops.
    round_two = await repo.try_acquire_gateway_lock(
        account.id, owner_id=_OWNER, now=_utcnow(), ttl=timedelta(seconds=90),
    )
    assert round_two is not None
    assert round_two.polling_last_error == first.message
    await repo.mark_gateway_success(account.id, owner_id=_OWNER, at=_utcnow())
    caplog.clear()

    with caplog.at_level(logging.DEBUG):
        again = await reporter.report(round_two, failure)

    assert again.logged is True
    assert len([r for r in caplog.records if r.name == CREDENTIAL_LOGGER_NAME]) == 1


async def test_caller_supplied_kind_wins_over_classification(caplog) -> None:
    """Telegram learns auth failure from a JSON ``error_code``, not an
    exception, so it must be able to declare the kind itself."""
    repo = InMemoryMessagingAccountRepository()
    account = await _locked_account(repo, platform=Platform.DISCORD)
    reporter = _reporter(repo, channel=MessagingErrorChannel.POLLING)

    with caplog.at_level(logging.DEBUG):
        report = await reporter.report(
            account,
            "Telegram rejected the bot token (error_code 401)",
            kind=MessagingFailureKind.CREDENTIAL,
        )

    assert report.kind is MessagingFailureKind.CREDENTIAL
    # A bare string cannot declare that only a credentials edit helps,
    # so the reporter answers on the safe side: keep retrying rather
    # than park the account on a signal that may never arrive.
    assert report.should_retry is True
    assert report.recorded is True
    records = [r for r in caplog.records if r.name == CREDENTIAL_LOGGER_NAME]
    assert len(records) == 1
    assert records[0].exc_info is None


async def test_plain_string_failure_defaults_to_transient_without_traceback(
    caplog,
) -> None:
    repo = InMemoryMessagingAccountRepository()
    account = await _locked_account(repo)
    reporter = _reporter(repo)

    with caplog.at_level(logging.DEBUG):
        report = await reporter.report(account, "Telegram returned HTTP 502")

    assert report.kind is MessagingFailureKind.TRANSIENT
    assert report.should_retry is True
    records = [r for r in caplog.records if r.name == _CALLER_LOGGER.name]
    assert len(records) == 1
    assert records[0].levelno == logging.ERROR
    # No exception object -> no bogus "NoneType: None" traceback tail.
    assert records[0].exc_info is None


async def test_structured_extra_fields_are_attached(caplog) -> None:
    repo = InMemoryMessagingAccountRepository()
    account = await _locked_account(repo)
    reporter = _reporter(repo)

    with caplog.at_level(logging.DEBUG):
        await reporter.report(
            account,
            MessagingCredentialError("Discord rejected the bot token"),
            context="gateway connect",
        )
        caplog_credential = [
            r for r in caplog.records if r.name == CREDENTIAL_LOGGER_NAME
        ]
        await reporter.report(account, RuntimeError("socket closed"))
        caplog_transient = [
            r for r in caplog.records if r.name == _CALLER_LOGGER.name
        ]

    assert len(caplog_credential) == 1
    assert caplog_credential[0].account_id == account.id
    assert caplog_credential[0].character_id == "char-1"
    assert caplog_credential[0].platform == Platform.DISCORD.value
    assert caplog_credential[0].failure_kind == MessagingFailureKind.CREDENTIAL.value
    assert "gateway connect" in caplog_credential[0].getMessage()

    assert len(caplog_transient) == 1
    assert caplog_transient[0].account_id == account.id
    assert caplog_transient[0].failure_kind == MessagingFailureKind.TRANSIENT.value


async def test_error_message_is_truncated_before_dedup_comparison() -> None:
    repo = InMemoryMessagingAccountRepository()
    account = await _locked_account(repo)
    reporter = _reporter(repo)

    first = await reporter.report(account, "x" * 2000)
    assert len(first.message) == 1000

    second = await reporter.report(account, "x" * 2000)
    assert second.logged is False


async def test_messages_differing_only_past_truncation_boundary_dedup(
    caplog,
) -> None:
    """Two *different* raw messages that agree on the first 1000 chars
    must collapse into "no change" — proving truncation happens before
    the stored-value comparison, not just that the same string twice is
    suppressed.

    Regression this pins: truncation length used to be a constructor
    parameter (``max_error_length``) independent of the length each
    repository truncates to on write. Nothing wired a different value
    through, so it silently worked — but had a caller ever passed one,
    the reporter would compare its own untruncated-to-that-length copy
    against a value the repository truncated to a different length,
    and de-duplication would never agree again for any message past
    both lengths. The fix removed the parameter in favour of the single
    module-level ``MAX_ERROR_LENGTH`` that every repository truncates
    to as well, which this test exercises indirectly through the
    in-memory repository's own (matching) truncation.
    """
    repo = InMemoryMessagingAccountRepository()
    account = await _locked_account(repo)
    reporter = _reporter(repo)

    with caplog.at_level(logging.DEBUG):
        first = await reporter.report(account, "x" * 1000 + "AAAA")
        caplog.clear()
        second = await reporter.report(account, "x" * 1000 + "BBBB")

    assert len(first.message) == 1000
    assert len(second.message) == 1000
    assert first.message == second.message
    assert second.logged is False
    assert caplog.records == []

    stored = await repo.get(account.id)
    assert stored is not None
    assert stored.polling_last_error == "x" * 1000


async def test_record_reports_false_when_lock_was_lost() -> None:
    repo = InMemoryMessagingAccountRepository()
    account = await _locked_account(repo)
    reporter = _reporter(repo)
    await repo.release_gateway_lock(account.id, owner_id=_OWNER)

    report = await reporter.report(account, RuntimeError("socket closed"))

    assert report.recorded is False
    stored = await repo.get(account.id)
    assert stored is not None
    assert stored.polling_last_error is None


async def test_deleted_account_mid_flight_does_not_raise(caplog) -> None:
    """A delete between acquiring the account and reporting must not
    take the worker loop down: the de-duplication baseline simply reads
    as "nothing stored"."""
    repo = InMemoryMessagingAccountRepository()
    account = await _locked_account(repo)
    reporter = _reporter(repo)
    assert await repo.delete(account.id) is True

    with caplog.at_level(logging.DEBUG):
        report = await reporter.report(account, RuntimeError("socket closed"))

    assert report.logged is True
    assert report.recorded is False
    assert len([r for r in caplog.records if r.name == _CALLER_LOGGER.name]) == 1


# --- retry axis: independent of the logging kind -------------------------


async def test_platform_side_credential_failure_keeps_retrying(caplog) -> None:
    """Discord close 4014 (*disallowed intents*) is the shape this pins.

    It is a credential-class failure for logging and for the UI, but the
    repair is a checkbox in the Developer Portal: the stored credentials
    never change, so "stop until the credentials change" would strand
    the account for the life of the process. Default construction —
    without ``credentials_must_change`` — must keep retrying.
    """
    repo = InMemoryMessagingAccountRepository()
    account = await _locked_account(repo)
    reporter = _reporter(repo)

    with caplog.at_level(logging.DEBUG):
        report = await reporter.report(
            account,
            MessagingCredentialError(
                "Discord refused the connection because this bot is missing the "
                "Message Content Intent. Enable it in the Developer Portal.",
            ),
        )

    assert report.kind is MessagingFailureKind.CREDENTIAL
    assert report.should_retry is True
    # Same logging treatment as the token case: dedicated logger,
    # WARNING, no traceback.
    records = [r for r in caplog.records if r.name == CREDENTIAL_LOGGER_NAME]
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert records[0].exc_info is None
    assert [r for r in caplog.records if r.name == _CALLER_LOGGER.name] == []


async def test_two_credential_failures_differ_only_in_retry(caplog) -> None:
    repo = InMemoryMessagingAccountRepository()
    account = await _locked_account(repo)
    reporter = _reporter(repo)

    with caplog.at_level(logging.DEBUG):
        editable = await reporter.report(
            account,
            MessagingCredentialError(
                "Discord rejected the bot token (HTTP 401).",
                credentials_must_change=True,
            ),
        )
        platform_side = await reporter.report(
            account,
            MessagingCredentialError("Discord disallowed the requested intents."),
        )

    assert editable.kind is platform_side.kind is MessagingFailureKind.CREDENTIAL
    assert editable.should_retry is False
    assert platform_side.should_retry is True
    records = [r for r in caplog.records if r.name == CREDENTIAL_LOGGER_NAME]
    assert len(records) == 2
    assert {r.levelno for r in records} == {logging.WARNING}
    assert {r.exc_info for r in records} == {None}


async def test_transient_failure_ignores_the_credential_retry_flag() -> None:
    repo = InMemoryMessagingAccountRepository()
    account = await _locked_account(repo)
    reporter = _reporter(repo)

    report = await reporter.report(account, TimeoutError("read timeout"))

    assert report.kind is MessagingFailureKind.TRANSIENT
    assert report.should_retry is True
