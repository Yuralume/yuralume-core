from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

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
