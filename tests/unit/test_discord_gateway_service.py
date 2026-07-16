import asyncio
from pathlib import Path
from typing import Any

import pytest

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

    async def connect(self, *, bot_token, on_message_create):  # noqa: ANN001
        self.tokens.append(bot_token)
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
