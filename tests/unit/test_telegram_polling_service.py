from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from kokoro_link.application.services.chat_turn_lease import ConversationBusyError
from kokoro_link.application.services.messaging_failure_reporter import (
    CREDENTIAL_LOGGER_NAME,
)
from kokoro_link.application.services.telegram_polling_service import (
    TelegramPollingService,
)
from kokoro_link.domain.entities.messaging_account import MessagingAccount
from kokoro_link.domain.value_objects.delivery_mode import DeliveryMode
from kokoro_link.domain.value_objects.platform import Platform
from kokoro_link.infrastructure.messaging.telegram.parser import parse_update
from tests.unit._messaging_harness import (
    build_messaging_harness,
    create_character,
    create_telegram_account,
)

_TRANSIENT_LOGGER_NAME = (
    "kokoro_link.application.services.telegram_polling_service"
)


class FakeTelegramPollingClient:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    async def get_updates(
        self,
        *,
        bot_token: str,
        offset: int | None = None,
        timeout_seconds: int = 25,
        limit: int = 100,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "bot_token": bot_token,
                "offset": offset,
                "timeout_seconds": timeout_seconds,
                "limit": limit,
            },
        )
        if self.responses:
            return self.responses.pop(0)
        return {"ok": True, "result": []}

    async def delete_webhook(
        self,
        *,
        bot_token: str,
        drop_pending_updates: bool = False,
    ) -> dict[str, Any]:
        return {"ok": True, "result": True}


async def _download_photo(**_: Any) -> str | None:
    return "/uploads/test-photo.png"


def _service(
    harness,
    client: FakeTelegramPollingClient,
    tmp_path: Path,
    *,
    owner_id: str = "worker-1",
) -> TelegramPollingService:
    return TelegramPollingService(
        account_repository=harness.account_repository,
        character_repository=harness.character_repository,
        dispatcher=harness.dispatcher,
        polling_client=client,
        update_parser=parse_update,
        photo_downloader=_download_photo,
        uploads_dir=tmp_path,
        owner_id=owner_id,
        long_poll_timeout_seconds=0,
    )


def _telegram_message_update(
    *,
    update_id: int,
    message_id: int,
    chat_id: int = 42,
    sender_id: int = 100,
    text: str = "早安",
) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": message_id,
            "date": 1_800_000_000,
            "chat": {"id": chat_id},
            "from": {"id": sender_id},
            "text": text,
        },
    }


@pytest.mark.asyncio
async def test_polling_account_dispatches_update_and_advances_offset(
    tmp_path: Path,
) -> None:
    harness = build_messaging_harness()
    character = await create_character(harness)
    account = await create_telegram_account(
        harness,
        character_id=character.id,
        delivery_mode=DeliveryMode.POLLING,
    )
    client = FakeTelegramPollingClient(
        [{"ok": True, "result": [_telegram_message_update(update_id=10, message_id=7)]}],
    )
    service = _service(harness, client, tmp_path)

    result = await service.poll_once()

    assert result.accounts_seen == 1
    assert result.acquired == 1
    assert result.updates_seen == 1
    assert result.dispatched == 1
    assert len(harness.telegram_adapter.sent) == 1
    binding = await harness.binding_repository.find(account.id, "42")
    assert binding is not None
    stored = await harness.account_repository.get(account.id)
    assert stored is not None
    assert stored.polling_offset == 11
    assert stored.polling_last_error is None


@pytest.mark.asyncio
async def test_polling_reuses_saved_offset_on_next_sweep(tmp_path: Path) -> None:
    harness = build_messaging_harness()
    character = await create_character(harness)
    account = await create_telegram_account(
        harness,
        character_id=character.id,
        delivery_mode=DeliveryMode.POLLING,
    )
    client = FakeTelegramPollingClient(
        [
            {"ok": True, "result": [_telegram_message_update(update_id=10, message_id=7)]},
            {"ok": True, "result": []},
        ],
    )
    service = _service(harness, client, tmp_path)

    await service.poll_once()
    await service.poll_once()

    assert [call["offset"] for call in client.calls] == [None, 11]


@pytest.mark.asyncio
async def test_disabled_account_is_not_polled(tmp_path: Path) -> None:
    harness = build_messaging_harness()
    character = await create_character(harness)
    account = await create_telegram_account(
        harness,
        character_id=character.id,
        delivery_mode=DeliveryMode.POLLING,
    )
    await harness.account_service.update(account.id, enabled=False)
    client = FakeTelegramPollingClient(
        [{"ok": True, "result": [_telegram_message_update(update_id=10, message_id=7)]}],
    )
    service = _service(harness, client, tmp_path)

    result = await service.poll_once()

    assert result.accounts_seen == 0
    assert client.calls == []


@pytest.mark.asyncio
async def test_allowlist_still_blocks_unlisted_sender_but_offset_advances(
    tmp_path: Path,
) -> None:
    harness = build_messaging_harness()
    character = await create_character(harness)
    account = await create_telegram_account(
        harness,
        character_id=character.id,
        allowed_sender_refs=("999",),
        delivery_mode=DeliveryMode.POLLING,
    )
    client = FakeTelegramPollingClient(
        [
            {
                "ok": True,
                "result": [
                    _telegram_message_update(
                        update_id=20, message_id=8, sender_id=100,
                    ),
                ],
            },
        ],
    )
    service = _service(harness, client, tmp_path)

    result = await service.poll_once()

    assert result.updates_seen == 1
    assert result.dispatched == 1
    assert harness.telegram_adapter.sent == []
    stored = await harness.account_repository.get(account.id)
    assert stored is not None
    assert stored.polling_offset == 21


@pytest.mark.asyncio
async def test_existing_db_lock_prevents_duplicate_polling(
    tmp_path: Path,
) -> None:
    harness = build_messaging_harness()
    character = await create_character(harness)
    account = await create_telegram_account(
        harness,
        character_id=character.id,
        delivery_mode=DeliveryMode.POLLING,
    )
    locked = await harness.account_repository.try_acquire_polling_lock(
        account.id,
        owner_id="other-worker",
        now=datetime.now(timezone.utc),
        ttl=timedelta(seconds=60),
    )
    assert locked is not None
    client = FakeTelegramPollingClient(
        [{"ok": True, "result": [_telegram_message_update(update_id=30, message_id=9)]}],
    )
    service = _service(harness, client, tmp_path, owner_id="worker-1")

    result = await service.poll_once()

    assert result.accounts_seen == 1
    assert result.acquired == 0
    assert client.calls == []


@pytest.mark.asyncio
async def test_duplicate_bot_token_accounts_are_not_polled(
    tmp_path: Path,
) -> None:
    harness = build_messaging_harness()
    first = await create_character(harness, name="Mio")
    second = await create_character(harness, name="Rin")
    first_account = await create_telegram_account(
        harness,
        character_id=first.id,
        bot_token="SAME",
        delivery_mode=DeliveryMode.POLLING,
    )
    second_account = MessagingAccount.create(
        character_id=second.id,
        platform=Platform.TELEGRAM,
        credentials={"bot_token": "SAME"},
        delivery_mode=DeliveryMode.POLLING,
    )
    await harness.account_repository.save(second_account)
    client = FakeTelegramPollingClient(
        [{"ok": True, "result": [_telegram_message_update(update_id=40, message_id=10)]}],
    )
    service = _service(harness, client, tmp_path)

    result = await service.poll_once()

    assert result.accounts_seen == 2
    assert result.acquired == 2
    assert result.updates_seen == 0
    assert result.dispatched == 0
    assert client.calls == []
    assert len(result.errors) == 2
    assert all("Duplicate Telegram bot token" in error for error in result.errors)
    stored_first = await harness.account_repository.get(first_account.id)
    stored_second = await harness.account_repository.get(second_account.id)
    assert stored_first is not None
    assert stored_second is not None
    assert "Duplicate Telegram bot token" in (stored_first.polling_last_error or "")
    assert "Duplicate Telegram bot token" in (stored_second.polling_last_error or "")


@pytest.mark.asyncio
async def test_getupdates_401_is_credential_and_logs_without_traceback(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    harness = build_messaging_harness()
    character = await create_character(harness)
    account = await create_telegram_account(
        harness,
        character_id=character.id,
        delivery_mode=DeliveryMode.POLLING,
    )
    client = FakeTelegramPollingClient(
        [{"ok": False, "error_code": 401, "description": "Unauthorized"}],
    )
    service = _service(harness, client, tmp_path)

    with caplog.at_level(logging.DEBUG):
        result = await service.poll_once()

    stored = await harness.account_repository.get(account.id)
    assert stored is not None
    # What reaches the account panel must be an instruction, not Telegram's
    # own word for its API contract: "Unauthorized" tells a self-hoster
    # nothing about which field to edit or where the token comes from.
    assert result.errors == (stored.polling_last_error,)
    assert "Unauthorized" not in (stored.polling_last_error or "")
    assert "bot token" in (stored.polling_last_error or "")
    assert "@BotFather" in (stored.polling_last_error or "")

    credential_records = [
        r for r in caplog.records if r.name == CREDENTIAL_LOGGER_NAME
    ]
    assert len(credential_records) == 1
    assert credential_records[0].levelno == logging.WARNING
    assert credential_records[0].exc_info is None
    assert credential_records[0].failure_kind == "credential"
    # The service's own logger must stay silent for credential failures —
    # no ERROR-with-traceback spam for a config problem only the account
    # owner can fix.
    assert [
        r for r in caplog.records
        if r.name == _TRANSIENT_LOGGER_NAME and r.levelno >= logging.ERROR
    ] == []


@pytest.mark.asyncio
async def test_getupdates_429_is_transient(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    harness = build_messaging_harness()
    character = await create_character(harness)
    account = await create_telegram_account(
        harness,
        character_id=character.id,
        delivery_mode=DeliveryMode.POLLING,
    )
    client = FakeTelegramPollingClient(
        [{"ok": False, "error_code": 429, "description": "Too Many Requests"}],
    )
    service = _service(harness, client, tmp_path)

    with caplog.at_level(logging.DEBUG):
        result = await service.poll_once()

    assert result.errors == ("Too Many Requests",)
    stored = await harness.account_repository.get(account.id)
    assert stored is not None
    assert stored.polling_last_error == "Too Many Requests"

    transient_records = [
        r for r in caplog.records if r.name == _TRANSIENT_LOGGER_NAME
    ]
    assert len(transient_records) == 1
    assert transient_records[0].levelno == logging.ERROR
    assert transient_records[0].failure_kind == "transient"
    assert [
        r for r in caplog.records if r.name == CREDENTIAL_LOGGER_NAME
    ] == []


@pytest.mark.asyncio
async def test_getupdates_429_keeps_polling_on_every_sweep(
    tmp_path: Path,
) -> None:
    """Rate limiting is the case retrying is *for*.

    The stop-retry memo keys on credentials, so a transient refusal must
    never reach it — parking on a 429 would take the account offline for
    the life of the process over a condition that clears by itself.
    """
    harness = build_messaging_harness()
    character = await create_character(harness)
    await create_telegram_account(
        harness,
        character_id=character.id,
        delivery_mode=DeliveryMode.POLLING,
    )
    client = FakeTelegramPollingClient(
        [
            {"ok": False, "error_code": 429, "description": "Too Many Requests"},
            {"ok": False, "error_code": 429, "description": "Too Many Requests"},
        ],
    )
    service = _service(harness, client, tmp_path)

    await service.poll_once()
    second = await service.poll_once()
    third = await service.poll_once()

    assert len(client.calls) == 3
    assert second.acquired == 1
    assert third.acquired == 1


@pytest.mark.asyncio
async def test_duplicate_bot_token_does_not_log_once_per_sweep(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Two accounts, three sweeps, two log lines — not six.

    A per-sweep summary WARNING on the module logger used to fire every
    2s no matter what, so silencing the dedicated credential logger did
    nothing for this condition. The per-account reporter line carries
    strictly more (the account id) and is de-duplicated.
    """
    harness = build_messaging_harness()
    first_character = await create_character(harness, name="Mio")
    second_character = await create_character(harness, name="Rin")
    await create_telegram_account(
        harness,
        character_id=first_character.id,
        bot_token="SAME",
        delivery_mode=DeliveryMode.POLLING,
    )
    await harness.account_repository.save(
        MessagingAccount.create(
            character_id=second_character.id,
            platform=Platform.TELEGRAM,
            credentials={"bot_token": "SAME"},
            delivery_mode=DeliveryMode.POLLING,
        ),
    )
    service = _service(harness, FakeTelegramPollingClient([]), tmp_path)

    with caplog.at_level(logging.DEBUG):
        await service.poll_once()
        await service.poll_once()
        third = await service.poll_once()

    assert len(
        [r for r in caplog.records if r.name == CREDENTIAL_LOGGER_NAME],
    ) == 2
    assert [
        r for r in caplog.records
        if r.name == _TRANSIENT_LOGGER_NAME and r.levelno >= logging.WARNING
    ] == []
    # Deleting the *other* account is the fix, so this one's credentials
    # never change — it must keep being re-evaluated, never parked.
    assert third.acquired == 2


@pytest.mark.asyncio
async def test_401_stops_calling_getupdates_on_later_sweeps(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A rejected token must cost one call, not one call every 2s.

    Re-asking Telegram with a token it answered 401 to cannot start
    working; at the poll cadence that is ~43k calls per broken account
    per day. Quieting the log was never the point — the request itself
    has to stop.
    """
    harness = build_messaging_harness()
    character = await create_character(harness)
    account = await create_telegram_account(
        harness,
        character_id=character.id,
        delivery_mode=DeliveryMode.POLLING,
    )
    client = FakeTelegramPollingClient(
        [{"ok": False, "error_code": 401, "description": "Unauthorized"}],
    )
    service = _service(harness, client, tmp_path)

    with caplog.at_level(logging.DEBUG):
        first = await service.poll_once()
        second = await service.poll_once()
        third = await service.poll_once()

    assert len(client.calls) == 1
    assert first.acquired == 1
    # Still a candidate (the row is untouched), just not worth polling.
    assert second.accounts_seen == 1
    assert second.acquired == 0
    assert third.acquired == 0
    assert len(
        [r for r in caplog.records if r.name == CREDENTIAL_LOGGER_NAME],
    ) == 1

    stored = await harness.account_repository.get(account.id)
    assert stored is not None
    assert stored.polling_last_error == first.errors[0]


@pytest.mark.asyncio
async def test_editing_the_bot_token_resumes_a_parked_account(
    tmp_path: Path,
) -> None:
    """The credentials fingerprint is the only un-park signal.

    Nothing is persisted about the park, so an operator pasting a new
    token has to be enough to resume on the very next sweep — otherwise
    a hosted player, who cannot restart the process, is stuck forever.
    """
    harness = build_messaging_harness()
    character = await create_character(harness)
    account = await create_telegram_account(
        harness,
        character_id=character.id,
        delivery_mode=DeliveryMode.POLLING,
    )
    client = FakeTelegramPollingClient(
        [{"ok": False, "error_code": 401, "description": "Unauthorized"}],
    )
    service = _service(harness, client, tmp_path)

    await service.poll_once()
    await service.poll_once()
    assert len(client.calls) == 1

    await harness.account_service.update(
        account.id, credentials={"bot_token": "FIXED-TOKEN"},
    )
    result = await service.poll_once()

    assert result.acquired == 1
    assert [call["bot_token"] for call in client.calls[1:]] == ["FIXED-TOKEN"]


@pytest.mark.asyncio
async def test_a_parked_account_leaving_the_candidate_list_is_forgotten(
    tmp_path: Path,
) -> None:
    """The memo is bounded by the accounts that still exist.

    Disabling (or deleting, or switching to webhook) drops the account
    from the sweep, and the entry must go with it rather than pinning
    process memory for accounts that may never come back.
    """
    harness = build_messaging_harness()
    character = await create_character(harness)
    account = await create_telegram_account(
        harness,
        character_id=character.id,
        delivery_mode=DeliveryMode.POLLING,
    )
    client = FakeTelegramPollingClient(
        [{"ok": False, "error_code": 401, "description": "Unauthorized"}],
    )
    service = _service(harness, client, tmp_path)

    await service.poll_once()
    await harness.account_service.update(account.id, enabled=False)
    await service.poll_once()
    await harness.account_service.update(account.id, enabled=True)
    result = await service.poll_once()

    # Same (still broken) token: re-confirming costs exactly one attempt.
    assert result.acquired == 1
    assert len(client.calls) == 2


@pytest.mark.asyncio
async def test_missing_bot_token_parks_the_account_with_an_actionable_message(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    harness = build_messaging_harness()
    character = await create_character(harness)
    # The entity refuses to *build* a tokenless Telegram account, so this
    # state can only arrive from a row that predates the invariant — which
    # is exactly why the polling loop still guards against it.
    account = replace(
        await create_telegram_account(
            harness,
            character_id=character.id,
            delivery_mode=DeliveryMode.POLLING,
        ),
        credentials={},
    )
    await harness.account_repository.save(account)
    client = FakeTelegramPollingClient([])
    service = _service(harness, client, tmp_path)

    with caplog.at_level(logging.DEBUG):
        first = await service.poll_once()
        second = await service.poll_once()

    assert client.calls == []
    assert second.acquired == 0
    stored = await harness.account_repository.get(account.id)
    assert stored is not None
    assert stored.polling_last_error == first.errors[0]
    assert "Missing bot_token" not in (stored.polling_last_error or "")
    assert "@BotFather" in (stored.polling_last_error or "")
    credential_records = [
        r for r in caplog.records if r.name == CREDENTIAL_LOGGER_NAME
    ]
    assert len(credential_records) == 1
    assert credential_records[0].exc_info is None


@pytest.mark.asyncio
async def test_busy_conversation_leaves_the_offset_for_the_next_sweep(
    tmp_path: Path,
) -> None:
    """Polling's re-delivery *is* the un-advanced offset.

    The dispatcher hands the delivery back (dedup stamps rolled back), so the
    sweep must stop before ``advance_polling_offset`` — the next poll ~2s later
    re-fetches the same update and runs it for real. It is also not an account
    error: nothing is wrong with the bot.
    """
    harness = build_messaging_harness()
    character = await create_character(harness)
    account = await create_telegram_account(
        harness,
        character_id=character.id,
        delivery_mode=DeliveryMode.POLLING,
    )

    async def _busy(*_args, **_kwargs):
        raise ConversationBusyError("conv-1")

    harness.chat_service.send_message = _busy
    client = FakeTelegramPollingClient(
        [{"ok": True, "result": [_telegram_message_update(update_id=10, message_id=7)]}],
    )
    service = _service(harness, client, tmp_path)

    result = await service.poll_once()

    assert result.dispatched == 0
    assert result.errors == ()
    stored = await harness.account_repository.get(account.id)
    assert stored is not None
    assert stored.polling_offset != 11
    assert stored.polling_last_error is None


@pytest.mark.asyncio
async def test_photo_owner_lookup_failure_does_not_store_or_dispatch(
    tmp_path: Path,
) -> None:
    harness = build_messaging_harness()
    character = await create_character(harness)
    account = await create_telegram_account(
        harness,
        character_id=character.id,
        delivery_mode=DeliveryMode.POLLING,
    )
    await harness.character_repository.delete(character.id)
    raw = _telegram_message_update(update_id=51, message_id=12)
    raw["message"]["photo"] = [{"file_id": "photo-1"}]
    downloaded_for: list[str] = []

    async def _record_download(**kwargs) -> str:
        downloaded_for.append(kwargs["user_id"])
        return "/should-not-exist.png"

    service = TelegramPollingService(
        account_repository=harness.account_repository,
        character_repository=harness.character_repository,
        dispatcher=harness.dispatcher,
        polling_client=FakeTelegramPollingClient(
            [{"ok": True, "result": [raw]}],
        ),
        update_parser=parse_update,
        photo_downloader=_record_download,
        uploads_dir=tmp_path,
        owner_id="worker-1",
        long_poll_timeout_seconds=0,
    )

    result = await service.poll_once()

    # The persisted/returned message is now the failure's own text (routed
    # through the reporter as the caught exception, not a synthesized
    # "Telegram update N processing failed" string) — see
    # ``test_update_processing_failure_logs_exactly_one_error_with_traceback``
    # for which update failed still being diagnosable via the log line.
    assert result.dispatched == 0
    assert result.errors == ("messaging media owner unavailable",)
    assert downloaded_for == []
    assert harness.telegram_adapter.sent == []
    assert await harness.binding_repository.find(account.id, "42") is None
    stored = await harness.account_repository.get(account.id)
    assert stored.polling_offset is None
    assert stored.polling_last_error == "messaging media owner unavailable"


@pytest.mark.asyncio
async def test_update_processing_failure_logs_exactly_one_error_with_traceback(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One broken update must cost one ERROR line, not two.

    The service used to keep its own ``_LOGGER.exception`` call *and* hand
    the same failure to the reporter, doubling every failed update into
    two ERROR lines. The reporter's own line must carry the traceback the
    manual call used to provide, plus the structured ``extra`` fields —
    and it must keep firing on every sweep the same broken update is
    retried: ``mark_polling_success`` clears the stored error right
    before this update fails again, so each sweep is reporting a failure
    that is genuinely happening right now, not a stale repeat that should
    be deduplicated away.
    """
    harness = build_messaging_harness()
    character = await create_character(harness)
    await create_telegram_account(
        harness,
        character_id=character.id,
        delivery_mode=DeliveryMode.POLLING,
    )
    raw = _telegram_message_update(update_id=51, message_id=12)

    def _raising_parser(_raw: dict[str, Any]) -> Any:
        # A parser failure keeps the failure site isolated to the
        # ``except Exception`` branch under test — unlike the photo-owner
        # lookup used elsewhere, ``resolve_messaging_media_owner`` logs its
        # own ERROR line before raising, which would pollute the count
        # this test is pinning.
        raise RuntimeError("parser exploded")

    client = FakeTelegramPollingClient(
        [
            {"ok": True, "result": [raw]},
            {"ok": True, "result": [raw]},
        ],
    )
    service = TelegramPollingService(
        account_repository=harness.account_repository,
        character_repository=harness.character_repository,
        dispatcher=harness.dispatcher,
        polling_client=client,
        update_parser=_raising_parser,
        photo_downloader=_download_photo,
        uploads_dir=tmp_path,
        owner_id="worker-1",
        long_poll_timeout_seconds=0,
    )

    with caplog.at_level(logging.DEBUG):
        first = await service.poll_once()
        second = await service.poll_once()

    assert first.dispatched == 0
    assert second.dispatched == 0
    error_records = [
        r for r in caplog.records
        if r.name == _TRANSIENT_LOGGER_NAME and r.levelno >= logging.ERROR
    ]
    # Two sweeps, two failures of the same stuck update, two ERROR lines —
    # one per failure. Not four (the old double-log bug) and not one (that
    # would mean the second, currently-happening failure got silently
    # deduplicated away).
    assert len(error_records) == 2
    for record in error_records:
        assert record.exc_info is not None
        assert record.failure_kind == "transient"
        assert "where=update 51 processing" in record.getMessage()
    assert [
        r for r in caplog.records if r.name == CREDENTIAL_LOGGER_NAME
    ] == []
