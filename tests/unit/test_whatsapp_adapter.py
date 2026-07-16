import json

import httpx
import pytest

from kokoro_link.contracts.messaging import OutboundAttachment, OutboundMessage
from kokoro_link.domain.value_objects.platform import Platform
from kokoro_link.infrastructure.messaging.whatsapp.adapter import WhatsAppAdapter


def _ok_transport(captured: list[httpx.Request]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"ok": True, "id": "m1"})

    return httpx.MockTransport(handler)


def _outbound(**overrides: object) -> OutboundMessage:
    defaults: dict[str, object] = {
        "platform": Platform.WHATSAPP,
        "chat_ref": "12025550123@s.whatsapp.net",
        "text": "hi",
        "credentials": {
            "sidecar_url": "http://127.0.0.1:32190/",
            "session_id": "mio",
            "api_token": "SIDE",
        },
    }
    defaults.update(overrides)
    return OutboundMessage(**defaults)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_send_posts_to_whatsapp_sidecar_session() -> None:
    captured: list[httpx.Request] = []
    adapter = WhatsAppAdapter(transport=_ok_transport(captured))

    await adapter.send(_outbound())

    assert len(captured) == 1
    request = captured[0]
    assert request.url.path == "/sessions/mio/messages"
    assert request.headers["authorization"] == "Bearer SIDE"
    body = json.loads(request.content.decode())
    assert body["chat_ref"] == "12025550123@s.whatsapp.net"
    assert body["text"] == "hi"
    assert body["attachments"] == []


@pytest.mark.asyncio
async def test_send_includes_attachment_metadata() -> None:
    captured: list[httpx.Request] = []
    adapter = WhatsAppAdapter(transport=_ok_transport(captured))

    await adapter.send(
        _outbound(
            attachments=(
                OutboundAttachment(
                    kind="image",
                    url="https://asset.test/a.png",
                    mime_type="image/png",
                    caption="reference",
                ),
            ),
        ),
    )

    body = json.loads(captured[0].content.decode())
    assert body["attachments"] == [
        {
            "kind": "image",
            "url": "https://asset.test/a.png",
            "mime_type": "image/png",
            "caption": "reference",
        },
    ]


@pytest.mark.asyncio
async def test_send_rejects_mismatched_platform() -> None:
    adapter = WhatsAppAdapter()
    with pytest.raises(ValueError):
        await adapter.send(_outbound(platform=Platform.LINE))


@pytest.mark.asyncio
async def test_send_skips_missing_sidecar_credentials(
    caplog: pytest.LogCaptureFixture,
) -> None:
    adapter = WhatsAppAdapter()
    with caplog.at_level("WARNING"):
        await adapter.send(_outbound(credentials={}))
    assert any("missing sidecar_url/session_id" in r.message for r in caplog.records)
