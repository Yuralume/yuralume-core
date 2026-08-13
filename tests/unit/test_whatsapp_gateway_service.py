import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest

from kokoro_link.application.services.messaging_failure_reporter import (
    CREDENTIAL_LOGGER_NAME,
)
from kokoro_link.application.services.whatsapp_gateway_service import (
    WhatsAppGatewayService,
)
from kokoro_link.infrastructure.messaging.whatsapp.parser import parse_whatsapp_event
from tests.unit._messaging_harness import (
    build_messaging_harness,
    create_character,
    create_whatsapp_account,
)


class _FakeSidecarClient:
    def __init__(self, event: dict[str, Any]) -> None:
        self.event = event
        self.connected = asyncio.Event()
        self.release = asyncio.Event()
        self.connections: list[tuple[str, str, str | None]] = []

    async def connect(
        self, *, sidecar_url, session_id, api_token, on_event,
        on_connected=None,
    ):  # noqa: ANN001
        self.connections.append((sidecar_url, session_id, api_token))
        if on_connected is not None:
            await on_connected()
        await on_event(self.event)
        self.connected.set()
        await self.release.wait()


@pytest.mark.asyncio
async def test_gateway_event_dispatches_through_messaging_pipeline() -> None:
    harness = build_messaging_harness()
    character = await create_character(harness)
    account = await create_whatsapp_account(
        harness,
        character_id=character.id,
        sidecar_url="http://127.0.0.1:32190/",
        session_id="mio",
        api_token="SIDE",
        allowed_sender_refs=("12025550123@s.whatsapp.net",),
    )
    sidecar = _FakeSidecarClient(
        {
            "id": "message-1",
            "chat_ref": "12025550123@s.whatsapp.net",
            "sender_ref": "12025550123@s.whatsapp.net",
            "text": "hello from whatsapp",
        },
    )
    service = WhatsAppGatewayService(
        account_repository=harness.account_repository,
        dispatcher=harness.dispatcher,
        sidecar_client=sidecar,
        event_parser=parse_whatsapp_event,
        sync_interval_seconds=999,
    )

    await service.sync_once()
    await asyncio.wait_for(sidecar.connected.wait(), timeout=1)

    assert sidecar.connections == [("http://127.0.0.1:32190", "mio", "SIDE")]
    assert len(harness.whatsapp_adapter.sent) == 1
    sent = harness.whatsapp_adapter.sent[0]
    assert sent.chat_ref == "12025550123@s.whatsapp.net"
    assert sent.credentials == account.credentials

    binding = await harness.binding_repository.find(
        account.id,
        "12025550123@s.whatsapp.net",
    )
    assert binding is not None
    assert binding.conversation_id is not None

    sidecar.release.set()
    await service.stop()


class _SidecarFailsBeforeConnected:
    async def connect(
        self, *, sidecar_url, session_id, api_token, on_event,
        on_connected=None,
    ):  # noqa: ANN001
        _ = (sidecar_url, session_id, api_token, on_event, on_connected)
        raise RuntimeError("SSE handshake failed")


@pytest.mark.asyncio
async def test_gateway_health_is_not_refreshed_before_sse_connected() -> None:
    harness = build_messaging_harness()
    character = await create_character(harness)
    account = await create_whatsapp_account(
        harness,
        character_id=character.id,
        sidecar_url="http://127.0.0.1:32190/",
        session_id="mio",
    )
    locked = await harness.account_repository.try_acquire_gateway_lock(
        account.id,
        owner_id="worker-1",
        now=datetime.now(timezone.utc),
        ttl=timedelta(seconds=90),
    )
    assert locked is not None
    service = WhatsAppGatewayService(
        account_repository=harness.account_repository,
        dispatcher=harness.dispatcher,
        sidecar_client=_SidecarFailsBeforeConnected(),
        event_parser=parse_whatsapp_event,
        owner_id="worker-1",
    )

    with pytest.raises(RuntimeError, match="SSE handshake failed"):
        await service._connect_locked_account(locked)

    stored = await harness.account_repository.get(account.id)
    assert stored.polling_last_update_at is None


class _SidecarRejectsToken:
    """Simulates the sidecar returning HTTP 401 for a wrong api_token."""

    def __init__(self) -> None:
        self.attempts = 0

    async def connect(
        self, *, sidecar_url, session_id, api_token, on_event,
        on_connected=None,
    ):  # noqa: ANN001
        self.attempts += 1
        request = httpx.Request("GET", f"{sidecar_url}/sessions/{session_id}/events")
        response = httpx.Response(401, request=request)
        raise httpx.HTTPStatusError(
            "Unauthorized", request=request, response=response,
        )


class _SidecarUnreachable:
    """Simulates connection-refused — the sidecar just isn't running yet."""

    def __init__(self) -> None:
        self.attempts = 0

    async def connect(
        self, *, sidecar_url, session_id, api_token, on_event,
        on_connected=None,
    ):  # noqa: ANN001
        self.attempts += 1
        request = httpx.Request("GET", f"{sidecar_url}/sessions/{session_id}/events")
        raise httpx.ConnectError("Connection refused", request=request)


async def _drain_account_task(
    service: WhatsAppGatewayService, account_id: str,
) -> None:
    task = service._tasks.get(account_id)
    assert task is not None, "expected the account loop to be running"
    await asyncio.wait_for(task, timeout=1)


@pytest.mark.asyncio
async def test_wrong_api_token_reports_credential_and_stops_retrying(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A rejected api_token must end the account loop, not reconnect forever.

    Mirrors the Discord gateway's shape: the loop exits after the single
    report instead of sleeping and re-checking the known-bad gate, so the
    task actually finishes and drops out of ``tasks_running`` on the next
    sync rather than staying alive to poll the DB every
    ``reconnect_delay_seconds``.
    """
    harness = build_messaging_harness()
    character = await create_character(harness)
    account = await create_whatsapp_account(
        harness,
        character_id=character.id,
        sidecar_url="http://127.0.0.1:32190",
        session_id="mio",
        api_token="WRONG",
    )
    sidecar = _SidecarRejectsToken()
    service = WhatsAppGatewayService(
        account_repository=harness.account_repository,
        dispatcher=harness.dispatcher,
        sidecar_client=sidecar,
        event_parser=parse_whatsapp_event,
        sync_interval_seconds=999,
        reconnect_delay_seconds=0,
    )

    with caplog.at_level(logging.DEBUG):
        await service.sync_once()
        await _drain_account_task(service, account.id)

    # The sidecar was dialed exactly once — the account loop returned
    # after the single credential report instead of reconnecting.
    assert sidecar.attempts == 1

    stored = await harness.account_repository.get(account.id)
    assert stored is not None
    assert "401" in stored.polling_last_error

    credential_records = [
        r for r in caplog.records if r.name == CREDENTIAL_LOGGER_NAME
    ]
    assert len(credential_records) == 1
    assert credential_records[0].levelno == logging.WARNING
    assert credential_records[0].exc_info is None

    # The account's own gateway lock must not be left held.
    relocked = await harness.account_repository.try_acquire_gateway_lock(
        account.id,
        owner_id="another-worker",
        now=datetime.now(timezone.utc),
        ttl=timedelta(seconds=90),
    )
    assert relocked is not None

    # A later sync must not resurrect a task for a known-bad account, and
    # must not count the finished task toward tasks_running.
    result = await service.sync_once()
    assert result.tasks_running == 0
    assert sidecar.attempts == 1


@pytest.mark.asyncio
async def test_known_bad_fingerprint_never_stores_plaintext_credentials() -> None:
    harness = build_messaging_harness()
    character = await create_character(harness)
    account = await create_whatsapp_account(
        harness,
        character_id=character.id,
        sidecar_url="http://127.0.0.1:32190",
        session_id="mio",
        api_token="SUPER-SECRET-TOKEN",
    )
    sidecar = _SidecarRejectsToken()
    service = WhatsAppGatewayService(
        account_repository=harness.account_repository,
        dispatcher=harness.dispatcher,
        sidecar_client=sidecar,
        event_parser=parse_whatsapp_event,
        sync_interval_seconds=999,
        reconnect_delay_seconds=0,
    )

    await service.sync_once()
    await _drain_account_task(service, account.id)

    assert account.id in service._known_bad_credentials
    fingerprint = service._known_bad_credentials[account.id]
    # Opaque sha256 digest, not the credentials (or any fragment of them).
    assert fingerprint != "SUPER-SECRET-TOKEN"
    assert "SUPER-SECRET-TOKEN" not in fingerprint
    assert "sidecar_url" not in fingerprint
    assert len(fingerprint) == 64
    assert all(c in "0123456789abcdef" for c in fingerprint)


@pytest.mark.asyncio
async def test_deleted_account_is_pruned_from_known_bad_memo() -> None:
    harness = build_messaging_harness()
    character = await create_character(harness)
    account = await create_whatsapp_account(
        harness,
        character_id=character.id,
        sidecar_url="http://127.0.0.1:32190",
        session_id="mio",
        api_token="WRONG",
    )
    sidecar = _SidecarRejectsToken()
    service = WhatsAppGatewayService(
        account_repository=harness.account_repository,
        dispatcher=harness.dispatcher,
        sidecar_client=sidecar,
        event_parser=parse_whatsapp_event,
        sync_interval_seconds=999,
        reconnect_delay_seconds=0,
    )

    await service.sync_once()
    await _drain_account_task(service, account.id)
    assert account.id in service._known_bad_credentials

    # Deleting the account must not pin its (plaintext-free) fingerprint —
    # and therefore the account it was derived from — for the process
    # lifetime.
    await harness.account_repository.delete(account.id)
    await service.sync_once()

    assert account.id not in service._known_bad_credentials


@pytest.mark.asyncio
async def test_repaired_credentials_resume_after_known_bad() -> None:
    harness = build_messaging_harness()
    character = await create_character(harness)
    account = await create_whatsapp_account(
        harness,
        character_id=character.id,
        sidecar_url="http://127.0.0.1:32190",
        session_id="mio",
        api_token="WRONG",
    )
    sidecar = _SidecarRejectsToken()
    service = WhatsAppGatewayService(
        account_repository=harness.account_repository,
        dispatcher=harness.dispatcher,
        sidecar_client=sidecar,
        event_parser=parse_whatsapp_event,
        sync_interval_seconds=999,
        reconnect_delay_seconds=0,
    )

    await service.sync_once()
    await _drain_account_task(service, account.id)
    assert sidecar.attempts == 1

    # Same credentials: no task, no reconnect, however often we sync.
    for _ in range(3):
        result = await service.sync_once()
        assert result.tasks_running == 0
    assert sidecar.attempts == 1

    # The operator edits the api_token: the fingerprint changes and the
    # account is eligible again without a restart.
    stored = await harness.account_repository.get(account.id)
    assert stored is not None
    await harness.account_repository.save(
        stored.with_credentials(
            {
                "sidecar_url": "http://127.0.0.1:32190",
                "session_id": "mio",
                "api_token": "REPAIRED",
            },
        ),
    )
    result = await service.sync_once()
    assert result.tasks_running == 1
    await _drain_account_task(service, account.id)
    assert sidecar.attempts == 2


@pytest.mark.asyncio
async def test_sidecar_unreachable_is_transient_and_logs_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    harness = build_messaging_harness()
    character = await create_character(harness)
    account = await create_whatsapp_account(
        harness,
        character_id=character.id,
        sidecar_url="http://127.0.0.1:32190",
        session_id="mio",
    )
    sidecar = _SidecarUnreachable()
    service = WhatsAppGatewayService(
        account_repository=harness.account_repository,
        dispatcher=harness.dispatcher,
        sidecar_client=sidecar,
        event_parser=parse_whatsapp_event,
        sync_interval_seconds=999,
        reconnect_delay_seconds=0,
    )

    with caplog.at_level(logging.DEBUG):
        task = asyncio.create_task(service._run_account_loop(account.id))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    # TRANSIENT keeps retrying — unlike the credential case, the sidecar
    # must have been dialed more than once in this window.
    assert sidecar.attempts > 1

    stored = await harness.account_repository.get(account.id)
    assert stored is not None
    assert "Connection refused" in stored.polling_last_error

    caller_records = [
        r for r in caplog.records
        if r.name == "kokoro_link.application.services.whatsapp_gateway_service"
    ]
    # Identical failure text every attempt -> de-duplicated to one log line,
    # even though the sidecar was dialed repeatedly.
    assert len(caller_records) == 1
    assert caller_records[0].levelno == logging.ERROR
    assert caller_records[0].exc_info is not None
    assert [
        r for r in caplog.records if r.name == CREDENTIAL_LOGGER_NAME
    ] == []


class _SidecarCancellingItsOwnLoop:
    """Puts a cancellation exactly in the loop's cleanup window.

    The account loop's ``finally`` does ``refresher.cancel()`` and then
    ``await refresher`` under a ``contextlib.suppress(CancelledError)``.
    That await is a genuine suspension point, so a cancellation arriving
    while the loop is between the failed connect and that await is
    delivered *there* — and the ``suppress`` cannot tell it from the
    refresher's own, so it swallows it and the loop carries on dialling a
    sidecar nobody is listening to any more.

    Cancelling from inside ``connect`` reproduces that window
    deterministically instead of racing a real ``stop()``: the request is
    latched while the task is running, and the first place it can be
    delivered is the ``await refresher``.

    Attempt 2 onwards raises ``CancelledError`` purely as a kill switch.
    It only ever runs when the leak is present, and without it the
    runaway loop outlives the test and can hang the event loop's teardown
    (this failure mode is a hang, not an assertion). The assertion that
    actually matters is ``attempts == 1``.
    """

    def __init__(self) -> None:
        self.attempts = 0

    async def connect(
        self, *, sidecar_url, session_id, api_token, on_event,
        on_connected=None,
    ):  # noqa: ANN001
        _ = (sidecar_url, session_id, api_token, on_event, on_connected)
        self.attempts += 1
        if self.attempts > 1:
            raise asyncio.CancelledError
        task = asyncio.current_task()
        assert task is not None
        task.cancel()
        raise RuntimeError("sidecar handshake failed")


@pytest.mark.asyncio
async def test_cancelling_the_account_loop_ends_it_and_frees_the_lock() -> None:
    """A cancelled account loop must actually stop, with the lock back.

    Production symptom of the leak: ``stop()`` /
    ``_cancel_all_account_tasks`` never returns at shutdown while the
    worker keeps hammering the sidecar. The failure is transient (a
    handshake error), so ``should_retry`` is ``True`` and the loop has no
    other reason to exit — the cancellation is the only thing that ends
    it.

    The lock assertions are the second half: the re-raise has to happen
    *after* ``release_gateway_lock``, otherwise a cancelled worker
    strands the account behind a held lock until its TTL expires.
    """
    harness = build_messaging_harness()
    character = await create_character(harness)
    account = await create_whatsapp_account(
        harness,
        character_id=character.id,
        sidecar_url="http://127.0.0.1:32190",
        session_id="mio",
    )
    sidecar = _SidecarCancellingItsOwnLoop()
    service = WhatsAppGatewayService(
        account_repository=harness.account_repository,
        dispatcher=harness.dispatcher,
        sidecar_client=sidecar,
        event_parser=parse_whatsapp_event,
        owner_id="worker-1",
        sync_interval_seconds=999,
        reconnect_delay_seconds=0,
    )

    task = asyncio.create_task(service._run_account_loop(account.id))
    # Bounded turns rather than `await task`: if the cancellation is
    # swallowed the task never finishes, and awaiting it would hang the
    # pytest process instead of failing this one test.
    for _ in range(50):
        if task.done():
            break
        await asyncio.sleep(0)

    assert sidecar.attempts == 1, (
        "the account loop survived its own cancellation and kept dialling "
        f"the sidecar ({sidecar.attempts} attempts)"
    )
    assert task.done()
    assert task.cancelled()

    stored = await harness.account_repository.get(account.id)
    assert stored is not None
    assert stored.polling_lock_owner is None
    assert stored.polling_lock_until is None
