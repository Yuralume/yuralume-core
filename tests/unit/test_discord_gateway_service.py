import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from kokoro_link.application.services.chat_turn_lease import ConversationBusyError
from kokoro_link.application.services.discord_gateway_service import (
    DiscordGatewayService,
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
