import asyncio
import logging
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from kokoro_link.application.services.chat_turn_lease import ConversationBusyError
from kokoro_link.application.services.discord_gateway_service import (
    DiscordGatewayService,
)
from kokoro_link.application.services.messaging_failure_reporter import (
    CREDENTIAL_LOGGER_NAME,
    MessagingFailureReport,
)
from kokoro_link.domain.value_objects.messaging_failure import (
    MessagingCredentialError,
    MessagingFailureKind,
    credentials_fingerprint,
)
from kokoro_link.infrastructure.messaging.discord.parser import parse_message_create
from tests.unit._messaging_harness import (
    build_messaging_harness,
    create_character,
    create_discord_account,
)


class _FakeGatewayClient:
    def __init__(self, message: dict[str, Any]) -> None:
        self.message = message
        self.connected = asyncio.Event()
        self.release = asyncio.Event()
        self.tokens: list[str] = []

    async def connect(
        self, *, bot_token, on_message_create, on_ready=None,
    ):  # noqa: ANN001
        self.tokens.append(bot_token)
        if on_ready is not None:
            await on_ready()
        await on_message_create(self.message, "bot-user")
        self.connected.set()
        await self.release.wait()


async def _download_attachment(**kwargs) -> str | None:  # noqa: ANN003
    return None


@pytest.mark.asyncio
async def test_gateway_message_dispatches_through_messaging_pipeline() -> None:
    harness = build_messaging_harness()
    character = await create_character(harness)
    account = await create_discord_account(
        harness,
        character_id=character.id,
        bot_token="DISCORD-TOKEN",
        allowed_sender_refs=("user-1",),
    )
    gateway = _FakeGatewayClient(
        {
            "id": "message-1",
            "channel_id": "channel-1",
            "content": "hello from discord",
            "author": {"id": "user-1"},
        },
    )
    service = DiscordGatewayService(
        account_repository=harness.account_repository,
        character_repository=harness.character_repository,
        dispatcher=harness.dispatcher,
        gateway_client=gateway,
        message_parser=parse_message_create,
        attachment_downloader=_download_attachment,
        uploads_dir=Path("."),
        sync_interval_seconds=999,
    )

    await service.sync_once()
    await asyncio.wait_for(gateway.connected.wait(), timeout=1)

    assert gateway.tokens == ["DISCORD-TOKEN"]
    assert len(harness.discord_adapter.sent) == 1
    sent = harness.discord_adapter.sent[0]
    assert sent.chat_ref == "channel-1"
    assert sent.credentials == account.credentials

    binding = await harness.binding_repository.find(account.id, "channel-1")
    assert binding is not None
    assert binding.conversation_id is not None

    gateway.release.set()
    await service.stop()


@pytest.mark.asyncio
async def test_busy_conversation_does_not_tear_down_the_gateway() -> None:
    """Discord never re-delivers, so an escaping error only costs the socket.

    The dispatcher re-raises ``ConversationBusyError`` for transports that can
    re-deliver; this connector cannot, so it must absorb it instead of letting
    it unwind the live Gateway connection (which would drop every other chat on
    the account and still not bring the message back).
    """
    harness = build_messaging_harness()
    character = await create_character(harness)
    await create_discord_account(
        harness,
        character_id=character.id,
        bot_token="DISCORD-TOKEN",
        allowed_sender_refs=("user-1",),
    )

    async def _busy(*_args, **_kwargs):
        raise ConversationBusyError("conv-1")

    harness.chat_service.send_message = _busy
    gateway = _FakeGatewayClient(
        {
            "id": "message-1",
            "channel_id": "channel-1",
            "content": "hello from discord",
            "author": {"id": "user-1"},
        },
    )
    service = DiscordGatewayService(
        account_repository=harness.account_repository,
        character_repository=harness.character_repository,
        dispatcher=harness.dispatcher,
        gateway_client=gateway,
        message_parser=parse_message_create,
        attachment_downloader=_download_attachment,
        uploads_dir=Path("."),
        sync_interval_seconds=999,
    )

    await service.sync_once()
    # ``connected`` is set only when ``on_message_create`` returned normally.
    await asyncio.wait_for(gateway.connected.wait(), timeout=1)

    assert harness.discord_adapter.sent == []

    gateway.release.set()
    await service.stop()


@pytest.mark.asyncio
async def test_attachment_owner_lookup_failure_does_not_store_or_dispatch(
    tmp_path: Path,
) -> None:
    harness = build_messaging_harness()
    character = await create_character(harness)
    account = await create_discord_account(
        harness,
        character_id=character.id,
        bot_token="DISCORD-TOKEN",
    )
    await harness.character_repository.delete(character.id)
    downloaded_for: list[str] = []

    async def _record_download(**kwargs) -> str:
        downloaded_for.append(kwargs["user_id"])
        return "/should-not-exist.png"

    service = DiscordGatewayService(
        account_repository=harness.account_repository,
        character_repository=harness.character_repository,
        dispatcher=harness.dispatcher,
        gateway_client=_FakeGatewayClient({}),
        message_parser=parse_message_create,
        attachment_downloader=_record_download,
        uploads_dir=tmp_path,
    )
    parsed = parse_message_create(
        {
            "id": "message-media",
            "channel_id": "channel-1",
            "content": "photo",
            "author": {"id": "user-1"},
            "attachments": [{"url": "https://cdn.test/photo.png"}],
        },
        bot_user_id="bot-user",
    )
    assert parsed is not None

    with pytest.raises(RuntimeError, match="media owner unavailable"):
        await service._build_inbound(account, parsed)

    assert downloaded_for == []
    assert harness.discord_adapter.sent == []
    assert await harness.binding_repository.find(account.id, "channel-1") is None


@pytest.mark.asyncio
async def test_media_owner_failure_drops_one_message_without_tearing_gateway(
    tmp_path: Path,
) -> None:
    harness = build_messaging_harness()
    character = await create_character(harness)
    account = await create_discord_account(
        harness,
        character_id=character.id,
        bot_token="DISCORD-TOKEN",
    )
    await harness.character_repository.delete(character.id)
    downloads = 0

    async def _record_download(**_kwargs) -> str:
        nonlocal downloads
        downloads += 1
        return "/should-not-exist.png"

    gateway = _FakeGatewayClient(
        {
            "id": "message-media",
            "channel_id": "channel-1",
            "content": "photo",
            "author": {"id": "user-1"},
            "attachments": [{"url": "https://cdn.test/photo.png"}],
        },
    )
    service = DiscordGatewayService(
        account_repository=harness.account_repository,
        character_repository=harness.character_repository,
        dispatcher=harness.dispatcher,
        gateway_client=gateway,
        message_parser=parse_message_create,
        attachment_downloader=_record_download,
        uploads_dir=tmp_path,
        owner_id="worker-1",
        sync_interval_seconds=999,
    )

    await service.sync_once()
    # Set only after the message callback returned: the socket stayed alive.
    await asyncio.wait_for(gateway.connected.wait(), timeout=1)

    assert downloads == 0
    assert harness.discord_adapter.sent == []
    assert await harness.binding_repository.find(account.id, "channel-1") is None
    stored = await harness.account_repository.get(account.id)
    assert stored.polling_last_error == "Inbound media owner unavailable"

    gateway.release.set()
    await service.stop()


class _DiscordFailsBeforeReady:
    async def connect(
        self, *, bot_token, on_message_create, on_ready=None,
    ):  # noqa: ANN001
        _ = (bot_token, on_message_create, on_ready)
        raise RuntimeError("handshake failed")


@pytest.mark.asyncio
async def test_gateway_health_is_not_refreshed_before_ready() -> None:
    harness = build_messaging_harness()
    character = await create_character(harness)
    account = await create_discord_account(
        harness,
        character_id=character.id,
        bot_token="DISCORD-TOKEN",
    )
    locked = await harness.account_repository.try_acquire_gateway_lock(
        account.id,
        owner_id="worker-1",
        now=datetime.now(timezone.utc),
        ttl=timedelta(seconds=90),
    )
    assert locked is not None
    service = DiscordGatewayService(
        account_repository=harness.account_repository,
        character_repository=harness.character_repository,
        dispatcher=harness.dispatcher,
        gateway_client=_DiscordFailsBeforeReady(),
        message_parser=parse_message_create,
        attachment_downloader=_download_attachment,
        uploads_dir=Path("."),
        owner_id="worker-1",
    )

    with pytest.raises(RuntimeError, match="handshake failed"):
        await service._connect_locked_account(locked)

    stored = await harness.account_repository.get(account.id)
    assert stored.polling_last_update_at is None


_REJECTED_TOKEN_MESSAGE = (
    "Discord rejected the bot token (HTTP 401). Check this account's Discord "
    "bot token."
)


class _CredentialRejectingClient:
    """Stands in for Discord refusing the credentials (HTTP 401 / close 4004).

    Counts connection attempts so tests can assert on *reconnects* rather
    than on sleeps, which is the behaviour that actually matters.

    ``credentials_must_change`` is what licenses the loop to stop: the
    token itself is what Discord refused, so an edit to the stored
    credentials is the signal that makes another attempt worth
    spending. A credential failure repaired on Discord's side (close
    4014, *disallowed intents*) carries no such signal, so it must keep
    retrying — construct with ``credentials_must_change=False`` to
    stand in for that shape.

    ``notify_after`` sets :attr:`reached` once that many attempts have
    happened, so a test can wait on *reconnects actually occurring*
    instead of sleeping and hoping.
    """

    def __init__(
        self,
        message: str = _REJECTED_TOKEN_MESSAGE,
        *,
        credentials_must_change: bool = True,
        notify_after: int = 0,
    ) -> None:
        self.message = message
        self._credentials_must_change = credentials_must_change
        self._notify_after = notify_after
        self.calls = 0
        self.reached = asyncio.Event()

    async def connect(
        self, *, bot_token, on_message_create, on_ready=None,
    ):  # noqa: ANN001
        _ = (bot_token, on_message_create, on_ready)
        self.calls += 1
        if self._notify_after and self.calls >= self._notify_after:
            self.reached.set()
        raise MessagingCredentialError(
            self.message,
            credentials_must_change=self._credentials_must_change,
        )


def _build_service(
    harness,  # noqa: ANN001
    gateway_client,  # noqa: ANN001
    *,
    owner_id: str = "worker-1",
) -> DiscordGatewayService:
    return DiscordGatewayService(
        account_repository=harness.account_repository,
        character_repository=harness.character_repository,
        dispatcher=harness.dispatcher,
        gateway_client=gateway_client,
        message_parser=parse_message_create,
        attachment_downloader=_download_attachment,
        uploads_dir=Path("."),
        owner_id=owner_id,
        sync_interval_seconds=999,
        # A retrying loop would spin without pausing, so any reconnect shows
        # up as a call count instead of a hang.
        reconnect_delay_seconds=0,
    )


async def _drain_account_task(service: DiscordGatewayService, account_id: str) -> None:
    task = service._tasks.get(account_id)
    assert task is not None, "expected the account loop to be running"
    await asyncio.wait_for(task, timeout=1)


async def _settle_account_task(
    service: DiscordGatewayService, account_id: str,
) -> None:
    """Await this sync's account loop if one was started, else return.

    Unlike :func:`_drain_account_task` this tolerates "no task" — which
    is the whole point once an account is parked, and lets a caller run
    the same tick repeatedly without asserting which side of the park it
    is on.
    """
    task = service._tasks.get(account_id)
    if task is not None:
        await asyncio.wait_for(task, timeout=1)


@pytest.mark.asyncio
async def test_credential_failure_stops_the_account_loop_and_frees_the_lock() -> None:
    """A rejected token must end the loop, not reconnect every 5 seconds.

    The old behaviour logged a full traceback per attempt forever; the
    account loop now exits after the single report, and — because the
    exit runs through the same ``finally`` — still hands the gateway lock
    back so another worker can take the account after the operator fixes
    it.
    """
    harness = build_messaging_harness()
    character = await create_character(harness)
    account = await create_discord_account(harness, character_id=character.id)
    client = _CredentialRejectingClient()
    service = _build_service(harness, client)

    await service.sync_once()
    await _drain_account_task(service, account.id)

    assert client.calls == 1
    stored = await harness.account_repository.get(account.id)
    assert stored.polling_last_error == _REJECTED_TOKEN_MESSAGE
    assert stored.polling_lock_owner is None
    assert stored.polling_lock_until is None


@pytest.mark.asyncio
async def test_rejected_credentials_are_skipped_until_they_change() -> None:
    harness = build_messaging_harness()
    character = await create_character(harness)
    account = await create_discord_account(harness, character_id=character.id)
    client = _CredentialRejectingClient()
    service = _build_service(harness, client)

    await service.sync_once()
    await _drain_account_task(service, account.id)
    assert client.calls == 1

    # Same credentials: no task, no reconnect, however often we sync.
    for _ in range(3):
        result = await service.sync_once()
        assert result.tasks_running == 0
    await asyncio.sleep(0)
    assert client.calls == 1

    # The operator edits the token: the fingerprint changes and the account
    # is eligible again without a restart.
    stored = await harness.account_repository.get(account.id)
    await harness.account_repository.save(
        stored.with_credentials({"bot_token": "REPAIRED-TOKEN"}),
    )
    result = await service.sync_once()
    assert result.tasks_running == 1
    await _drain_account_task(service, account.id)
    assert client.calls == 2


@pytest.mark.asyncio
async def test_credential_failure_logs_once_without_a_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    harness = build_messaging_harness()
    character = await create_character(harness)
    account = await create_discord_account(harness, character_id=character.id)
    client = _CredentialRejectingClient()
    service = _build_service(harness, client)

    with caplog.at_level(logging.WARNING):
        await service.sync_once()
        await _drain_account_task(service, account.id)

        records = [r for r in caplog.records if r.name == CREDENTIAL_LOGGER_NAME]
        assert len(records) == 1
        assert records[0].levelno == logging.WARNING
        assert records[0].exc_info is None
        assert records[0].failure_kind == "credential"
        assert records[0].platform == "discord"
        assert records[0].account_id == account.id
        assert records[0].character_id == character.id
        # Nobody else shouted about it — no traceback on the module logger.
        assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []

        # A second wrong token that fails identically is the same line: the
        # stored polling_last_error already says it, so it stays quiet.
        stored = await harness.account_repository.get(account.id)
        await harness.account_repository.save(
            stored.with_credentials({"bot_token": "ANOTHER-BAD-TOKEN"}),
        )
        await service.sync_once()
        await _drain_account_task(service, account.id)

    assert client.calls == 2
    assert len([r for r in caplog.records if r.name == CREDENTIAL_LOGGER_NAME]) == 1


@pytest.mark.asyncio
async def test_duplicate_bot_token_is_reported_once_across_syncs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The duplicate guard re-runs every sync; it must not re-log every sync."""
    harness = build_messaging_harness()
    first_character = await create_character(harness, name="Mio")
    second_character = await create_character(harness, name="Rin")
    first = await create_discord_account(
        harness, character_id=first_character.id, bot_token="SHARED-TOKEN",
    )
    second = await create_discord_account(
        harness, character_id=second_character.id, bot_token="OTHER-TOKEN",
    )
    # The service-level guard blocks creating a second account on the same
    # token, so collide them directly in the repository.
    await harness.account_repository.save(
        second.with_credentials({"bot_token": "SHARED-TOKEN"}),
    )
    service = _build_service(harness, _CredentialRejectingClient())

    with caplog.at_level(logging.WARNING):
        result = await service.sync_once()
        assert result.duplicates == 2
        assert result.tasks_running == 0
        first_pass = [r for r in caplog.records if r.name == CREDENTIAL_LOGGER_NAME]
        assert len(first_pass) == 2  # one per colliding account

        for _ in range(3):
            await service.sync_once()

    records = [r for r in caplog.records if r.name == CREDENTIAL_LOGGER_NAME]
    assert len(records) == 2
    stored = await harness.account_repository.get(first.id)
    assert stored.polling_last_error is not None
    assert "Duplicate Discord bot token" in stored.polling_last_error


@pytest.mark.asyncio
async def test_missing_bot_token_is_a_credential_failure() -> None:
    harness = build_messaging_harness()
    character = await create_character(harness)
    account = await create_discord_account(harness, character_id=character.id)
    stored = await harness.account_repository.get(account.id)
    # ``with_credentials`` refuses an empty token, so bypass the entity guard
    # to reproduce a row that lost its token.
    await harness.account_repository.save(
        replace(stored, credentials={"bot_token": ""}),
    )
    client = _CredentialRejectingClient()
    service = _build_service(harness, client)

    await service.sync_once()
    await _drain_account_task(service, account.id)

    assert client.calls == 0  # never even dialled Discord
    stored = await harness.account_repository.get(account.id)
    assert stored.polling_last_error is not None
    assert "no bot token" in stored.polling_last_error
    assert (await service.sync_once()).tasks_running == 0


_DISALLOWED_INTENTS_MESSAGE = (
    "Discord refused the requested gateway intents (close 4014). Enable "
    "Message Content Intent; the bot token itself is fine."
)


@pytest.mark.asyncio
async def test_platform_side_credential_failure_keeps_reconnecting(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Close 4014 is a credential failure that must NOT park the account.

    Message Content Intent is off by default, so 4014 is what a brand new
    bot gets on its first connection. Its repair is a checkbox in the
    Developer Portal, which leaves the stored token byte-identical — so
    parking "until the credentials change" would wait on an edit that
    correctly never comes, and a hosted player cannot restart the process
    to escape it. It stays a CREDENTIAL failure for logging and for the
    UI; only the retry decision differs.
    """
    harness = build_messaging_harness()
    character = await create_character(harness)
    account = await create_discord_account(harness, character_id=character.id)
    client = _CredentialRejectingClient(
        _DISALLOWED_INTENTS_MESSAGE,
        credentials_must_change=False,
        notify_after=3,
    )
    service = _build_service(harness, client)

    with caplog.at_level(logging.WARNING):
        await service.sync_once()
        await asyncio.wait_for(client.reached.wait(), timeout=1)

        assert client.calls >= 3  # it really did reconnect, not just once
        task = service._tasks[account.id]
        assert not task.done()
        # Still a runnable account on every later sync: never parked.
        assert (await service.sync_once()).tasks_running == 1

        # Logged exactly like a rejected token would be: dedicated logger,
        # WARNING, no traceback, and only the first occurrence.
        records = [r for r in caplog.records if r.name == CREDENTIAL_LOGGER_NAME]
        assert len(records) == 1
        assert records[0].levelno == logging.WARNING
        assert records[0].exc_info is None
        assert records[0].failure_kind == "credential"
        assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []

    await service.stop()


@pytest.mark.asyncio
async def test_stop_ends_a_loop_that_is_still_retrying() -> None:
    """``stop()`` must win against a loop that never parks itself.

    The loop cancels its lock refresher and awaits it inside a
    ``finally``; that await is a real suspension point, so a ``stop()``
    landing there raises a ``CancelledError`` the surrounding
    ``suppress`` cannot tell from the refresher's own — and swallows.
    Before a credential failure was allowed to keep retrying, every loop
    exit either parked or sat inside a live connection, so this window
    was never hit. A 4014 account spins through it forever: without the
    re-raise, ``stop()`` hangs and the worker keeps dialling Discord
    after shutdown.
    """
    harness = build_messaging_harness()
    character = await create_character(harness)
    account = await create_discord_account(harness, character_id=character.id)
    client = _CredentialRejectingClient(
        _DISALLOWED_INTENTS_MESSAGE,
        credentials_must_change=False,
        notify_after=2,
    )
    service = _build_service(harness, client)

    await service.sync_once()
    await asyncio.wait_for(client.reached.wait(), timeout=1)
    task = service._tasks[account.id]
    assert not task.done()

    await asyncio.wait_for(service.stop(), timeout=2)

    assert task.done()
    settled = client.calls
    await asyncio.sleep(0)
    assert client.calls == settled  # really stopped, not merely detached
    # The lock came back on the way out, so another worker can take over.
    stored = await harness.account_repository.get(account.id)
    assert stored.polling_lock_owner is None


@pytest.mark.asyncio
async def test_parking_is_not_undone_by_its_own_error_write() -> None:
    """The park must outlive the writes that happen while parking.

    Reporting the failure writes ``polling_last_error`` and stamps
    ``updated_at``, and the lock release writes again. None of that
    touches ``credentials``, which is the only field the memo reads — so
    the park is built from the entity the loop was already holding and
    the row moving underneath it changes nothing.
    """
    harness = build_messaging_harness()
    character = await create_character(harness)
    account = await create_discord_account(harness, character_id=character.id)
    client = _CredentialRejectingClient()
    service = _build_service(harness, client)

    await service.sync_once()
    await _drain_account_task(service, account.id)
    assert client.calls == 1

    stored = await harness.account_repository.get(account.id)
    assert stored.polling_last_error is not None  # the write really happened
    assert service._parked[account.id] == credentials_fingerprint(stored.credentials)

    for _ in range(3):
        assert (await service.sync_once()).tasks_running == 0
    await asyncio.sleep(0)
    assert client.calls == 1


@pytest.mark.asyncio
async def test_resaving_without_a_token_change_does_not_unpark() -> None:
    """A save that changes no credential buys a retry that must fail.

    This replaces ``test_saving_the_account_unparks_it_without_a_token
    _change``, which pinned the opposite: parking used to also remember
    the row's ``updated_at`` and lift as soon as it moved, on the theory
    that re-saving the account is the operator saying "I fixed it
    elsewhere, try again". ``updated_at`` is not an operator-only signal
    — ``try_acquire_gateway_lock`` stamps it on every attempt by every
    connector instance — so with Hosted now running two connectors that
    rule made the two instances un-park each other forever. The signal is
    the credentials fingerprint alone, and this test pins that a
    no-op save is *not* it.

    Nothing is lost: a repair that leaves the token byte-identical is
    close 4014 (*disallowed intents*), which never parks in the first
    place (``credentials_must_change=False``) and keeps retrying under
    the backoff — see
    ``test_platform_side_credential_failure_keeps_reconnecting``.
    """
    harness = build_messaging_harness()
    character = await create_character(harness)
    account = await create_discord_account(harness, character_id=character.id)
    client = _CredentialRejectingClient()
    service = _build_service(harness, client)

    await service.sync_once()
    await _drain_account_task(service, account.id)
    assert (await service.sync_once()).tasks_running == 0
    assert client.calls == 1

    parked_at = await harness.account_repository.get(account.id)
    await harness.account_repository.save(
        parked_at.with_display_name(
            "Mio bot",
            now=parked_at.updated_at + timedelta(seconds=1),
        ),
    )
    resaved = await harness.account_repository.get(account.id)
    assert resaved.credentials == parked_at.credentials  # token untouched
    assert resaved.updated_at != parked_at.updated_at  # the row really moved

    assert (await service.sync_once()).tasks_running == 0
    await asyncio.sleep(0)
    assert client.calls == 1

    # …and the signal that *does* lift it still works.
    await harness.account_repository.save(
        resaved.with_credentials({"bot_token": "A-FRESH-TOKEN"}),
    )
    assert (await service.sync_once()).tasks_running == 1
    await _drain_account_task(service, account.id)
    assert client.calls == 2


@pytest.mark.asyncio
async def test_two_connector_instances_each_pay_exactly_one_attempt() -> None:
    """Parking must bound retries by fleet size, not by uptime.

    Hosted runs two of every role, connector included, so two
    ``DiscordGatewayService`` instances sync the same broken account
    against the same rows. Each keeps its own in-memory memo, so each
    spends one attempt and then goes quiet: N instances = N attempts,
    total, forever.

    This is the regression guard for the ``updated_at`` resume signal.
    While the park also lifted when the row's ``updated_at`` moved, the
    two instances un-parked each other every tick — ``try_acquire_gateway
    _lock``'s UPDATE stamps ``updated_at``, so A read B's lock attempt as
    "the operator repaired it" and vice versa — and a rejected token was
    back to reconnecting on every sync. Silently: the failure reporter
    de-duplicates, so the logs showed one WARNING either way. Only a
    count of connection attempts can see it, which is why this test
    asserts on ``client.calls`` and not on log records.
    """
    harness = build_messaging_harness()
    character = await create_character(harness)
    account = await create_discord_account(harness, character_id=character.id)
    client = _CredentialRejectingClient()
    instances = [
        _build_service(harness, client, owner_id="connector-1"),
        _build_service(harness, client, owner_id="connector-2"),
    ]

    for _ in range(8):
        for instance in instances:
            await instance.sync_once()
            await _settle_account_task(instance, account.id)

    assert client.calls == len(instances)
    for instance in instances:
        assert (await instance.sync_once()).tasks_running == 0
    # The last one out still handed the lease back.
    stored = await harness.account_repository.get(account.id)
    assert stored.polling_lock_owner is None


def _credential_report(should_retry: bool) -> MessagingFailureReport:
    return MessagingFailureReport(
        kind=MessagingFailureKind.CREDENTIAL,
        message=_DISALLOWED_INTENTS_MESSAGE,
        should_retry=should_retry,
        logged=True,
        recorded=True,
    )


def _backoff_service() -> DiscordGatewayService:
    harness = build_messaging_harness()
    return DiscordGatewayService(
        account_repository=harness.account_repository,
        character_repository=harness.character_repository,
        dispatcher=harness.dispatcher,
        gateway_client=_CredentialRejectingClient(),
        message_parser=parse_message_create,
        attachment_downloader=_download_attachment,
        uploads_dir=Path("."),
        sync_interval_seconds=999,
        reconnect_delay_seconds=5.0,
        credential_retry_max_delay_seconds=60.0,
    )


def test_repeated_retryable_credential_failures_back_off_and_cap() -> None:
    """A doomed handshake every 5s would burn Discord's IDENTIFY budget.

    Each 4014 attempt is a ``/gateway/bot`` call plus a WebSocket
    connect plus an IDENTIFY, and IDENTIFY is rate-limited per day
    (1000 for an unsharded bot) — a fixed 5s retry spends ~17k of them
    daily and can get the token throttled on top of the original
    problem. The cap keeps recovery latency bounded at one minute.
    """
    service = _backoff_service()
    report = _credential_report(should_retry=True)

    delays = [service._retry_delay_for("account-1", report) for _ in range(6)]

    assert delays == [5.0, 10.0, 20.0, 40.0, 60.0, 60.0]


def test_backoff_is_per_account() -> None:
    service = _backoff_service()
    report = _credential_report(should_retry=True)

    service._retry_delay_for("account-1", report)
    service._retry_delay_for("account-1", report)

    assert service._retry_delay_for("account-2", report) == 5.0


def test_any_non_credential_outcome_resets_the_backoff() -> None:
    """Recovery must not inherit the penalty of the failures before it."""
    service = _backoff_service()
    report = _credential_report(should_retry=True)
    for _ in range(4):
        service._retry_delay_for("account-1", report)

    # ``None`` is the shape of "the connection ran and then closed".
    assert service._retry_delay_for("account-1", None) == 5.0
    assert service._retry_delay_for("account-1", report) == 5.0
