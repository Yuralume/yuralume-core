import asyncio
from typing import Any

import pytest

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

    async def connect(self, *, sidecar_url, session_id, api_token, on_event):  # noqa: ANN001
        self.connections.append((sidecar_url, session_id, api_token))
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
