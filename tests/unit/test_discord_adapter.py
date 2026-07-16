import json

import httpx
import pytest

from kokoro_link.contracts.messaging import OutboundAttachment, OutboundMessage
from kokoro_link.domain.value_objects.platform import Platform
from kokoro_link.infrastructure.messaging.discord.adapter import DiscordAdapter


def _ok_transport(captured: list[httpx.Request]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"id": "m1"})

    return httpx.MockTransport(handler)


def _outbound(**overrides: object) -> OutboundMessage:
    defaults: dict[str, object] = {
        "platform": Platform.DISCORD,
        "chat_ref": "channel-1",
        "text": "hi",
        "credentials": {"bot_token": "TOKEN"},
    }
    defaults.update(overrides)
    return OutboundMessage(**defaults)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_send_posts_discord_message_with_bot_token() -> None:
    captured: list[httpx.Request] = []
    adapter = DiscordAdapter(transport=_ok_transport(captured))

    await adapter.send(_outbound())

    assert len(captured) == 1
    request = captured[0]
    assert request.url.path == "/api/v10/channels/channel-1/messages"
    assert request.headers["authorization"] == "Bot TOKEN"
    body = json.loads(request.content.decode())
    assert body["content"] == "hi"
    assert body["allowed_mentions"] == {"parse": []}


@pytest.mark.asyncio
async def test_send_many_delivers_bubbles_sequentially() -> None:
    """Discord has no batch endpoint — a batched hand-over must behave
    exactly like the old per-bubble loop, order preserved."""
    captured: list[httpx.Request] = []
    adapter = DiscordAdapter(transport=_ok_transport(captured))

    await adapter.send_many([_outbound(text="第一則"), _outbound(text="第二則")])

    assert len(captured) == 2
    contents = [
        json.loads(r.content.decode())["content"] for r in captured
    ]
    assert contents == ["第一則", "第二則"]


@pytest.mark.asyncio
async def test_send_appends_attachment_urls_to_text() -> None:
    captured: list[httpx.Request] = []
    adapter = DiscordAdapter(transport=_ok_transport(captured))

    await adapter.send(
        _outbound(
            attachments=(
                OutboundAttachment(kind="image", url="https://asset.test/a.png"),
            ),
        ),
    )

    body = json.loads(captured[0].content.decode())
    assert "hi" in body["content"]
    assert "https://asset.test/a.png" in body["content"]


@pytest.mark.asyncio
async def test_send_preserves_attachment_url_when_caption_exists() -> None:
    captured: list[httpx.Request] = []
    adapter = DiscordAdapter(transport=_ok_transport(captured))

    await adapter.send(
        _outbound(
            attachments=(
                OutboundAttachment(
                    kind="image",
                    url="https://asset.test/a.png",
                    caption="reference image",
                ),
            ),
        ),
    )

    body = json.loads(captured[0].content.decode())
    assert "reference image" in body["content"]
    assert "https://asset.test/a.png" in body["content"]


@pytest.mark.asyncio
async def test_send_rejects_mismatched_platform() -> None:
    adapter = DiscordAdapter()
    with pytest.raises(ValueError):
        await adapter.send(_outbound(platform=Platform.LINE))


@pytest.mark.asyncio
async def test_send_skips_missing_token(caplog: pytest.LogCaptureFixture) -> None:
    adapter = DiscordAdapter()
    with caplog.at_level("WARNING"):
        await adapter.send(_outbound(credentials={}))
    assert any("missing bot_token" in r.message for r in caplog.records)
