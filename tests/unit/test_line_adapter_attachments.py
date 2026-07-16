"""BDD for LINE outbound image attachments.

LINE's push API takes up to 5 message objects per call. We pack text
+ images into a single call and verify the shape.
"""

from __future__ import annotations

import json

import httpx
import pytest

from kokoro_link.contracts.messaging import (
    OutboundAttachment,
    OutboundMessage,
)
from kokoro_link.domain.value_objects.platform import Platform
from kokoro_link.infrastructure.messaging.line.adapter import LineAdapter


def _ok_transport(captured: list[httpx.Request]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={})

    return httpx.MockTransport(handler)


def _outbound(**overrides: object) -> OutboundMessage:
    defaults: dict[str, object] = {
        "platform": Platform.LINE,
        "chat_ref": "U42",
        "text": "",
        "credentials": {"channel_access_token": "AT"},
        "attachments": (),
    }
    defaults.update(overrides)
    return OutboundMessage(**defaults)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_text_and_image_bundled_in_one_push() -> None:
    captured: list[httpx.Request] = []
    adapter = LineAdapter(transport=_ok_transport(captured))

    await adapter.send(_outbound(
        text="這張",
        attachments=(
            OutboundAttachment(
                kind="image", url="https://ex.com/a.png",
                mime_type="image/png",
            ),
        ),
    ))

    assert len(captured) == 1
    body = json.loads(captured[0].content.decode())
    assert body["to"] == "U42"
    assert body["messages"] == [
        {"type": "text", "text": "這張"},
        {
            "type": "image",
            "originalContentUrl": "https://ex.com/a.png",
            "previewImageUrl": "https://ex.com/a.png",
        },
    ]


@pytest.mark.asyncio
async def test_multiple_images_capped_at_five_messages() -> None:
    captured: list[httpx.Request] = []
    adapter = LineAdapter(transport=_ok_transport(captured))

    # 1 text + 5 images → should be truncated to 5 messages total.
    await adapter.send(_outbound(
        text="多張",
        attachments=tuple(
            OutboundAttachment(
                kind="image", url=f"https://ex.com/{i}.png",
                mime_type="image/png",
            )
            for i in range(5)
        ),
    ))

    body = json.loads(captured[0].content.decode())
    assert len(body["messages"]) == 5
    # The 5th attachment must have been dropped — the text already
    # consumed one slot.
    assert body["messages"][0]["type"] == "text"
    image_urls = [m.get("originalContentUrl") for m in body["messages"][1:]]
    assert image_urls == [f"https://ex.com/{i}.png" for i in range(4)]


@pytest.mark.asyncio
async def test_empty_message_skipped() -> None:
    captured: list[httpx.Request] = []
    adapter = LineAdapter(transport=_ok_transport(captured))

    await adapter.send(_outbound(text="", attachments=()))

    assert captured == []


@pytest.mark.asyncio
async def test_non_image_attachment_becomes_text() -> None:
    captured: list[httpx.Request] = []
    adapter = LineAdapter(transport=_ok_transport(captured))

    await adapter.send(_outbound(
        text="附件",
        attachments=(
            OutboundAttachment(
                kind="file", url="https://ex.com/doc.pdf", caption="報告",
            ),
        ),
    ))

    body = json.loads(captured[0].content.decode())
    assert body["messages"] == [
        {"type": "text", "text": "附件"},
        {"type": "text", "text": "附件：報告"},
    ]


@pytest.mark.asyncio
async def test_non_image_attachment_label_localized_for_en_operator() -> None:
    """The channel-wrapper text (not the character's own reply) must
    follow the operator locale so an en-US operator's LINE chat doesn't
    show a zh-TW '附件：' prefix."""
    captured: list[httpx.Request] = []
    adapter = LineAdapter(transport=_ok_transport(captured))

    await adapter.send(_outbound(
        text="",
        locale="en-US",
        attachments=(
            OutboundAttachment(
                kind="file", url="https://ex.com/doc.pdf", caption="report",
            ),
        ),
    ))

    body = json.loads(captured[0].content.decode())
    note = body["messages"][0]["text"]
    assert "附件" not in note
    assert note == "Attachment: report"


@pytest.mark.asyncio
async def test_invalid_image_url_note_localized_for_ja_operator() -> None:
    captured: list[httpx.Request] = []
    adapter = LineAdapter(transport=_ok_transport(captured))

    await adapter.send(_outbound(
        text="",
        locale="ja-JP",
        attachments=(
            OutboundAttachment(
                kind="image",
                url="http://insecure.example.com/a.png",
                mime_type="image/png",
            ),
        ),
    ))

    body = json.loads(captured[0].content.decode())
    note = body["messages"][0]["text"]
    # zh-TW wrapper must not leak; ja wrapper mentions LINE requirements.
    assert "不符 LINE 要求" not in note
    assert "LINE" in note


# ---------- URL pre-flight validation (Slice-7 follow-up) ----------


@pytest.mark.asyncio
async def test_http_image_url_is_rejected_with_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """http:// URL would be silently 400'd by LINE. We catch it early,
    log the reason, and fall back to a text note so the user still sees
    *something*."""
    captured: list[httpx.Request] = []
    adapter = LineAdapter(transport=_ok_transport(captured))

    with caplog.at_level("WARNING"):
        await adapter.send(_outbound(
            text="這張",
            attachments=(
                OutboundAttachment(
                    kind="image",
                    url="http://insecure.example.com/a.png",
                    mime_type="image/png",
                ),
            ),
        ))

    # Still one push call — attachment replaced, not dropped.
    assert len(captured) == 1
    body = json.loads(captured[0].content.decode())
    assert body["messages"][0] == {"type": "text", "text": "這張"}
    # Fallback text note mentions the offending URL for debuggability
    assert body["messages"][1]["type"] == "text"
    assert "http://insecure.example.com/a.png" in body["messages"][1]["text"]
    # Warning log carries the specific reason
    assert any(
        "reason=" in r.message and "https" in r.message
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_webp_image_url_is_rejected(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """LINE only accepts JPEG/PNG in image messages — .webp would 400."""
    captured: list[httpx.Request] = []
    adapter = LineAdapter(transport=_ok_transport(captured))

    with caplog.at_level("WARNING"):
        await adapter.send(_outbound(
            text="",
            attachments=(
                OutboundAttachment(
                    kind="image",
                    url="https://cdn.example.com/a.webp",
                    mime_type="image/webp",
                ),
            ),
        ))

    # Text fallback still goes out
    assert len(captured) == 1
    body = json.loads(captured[0].content.decode())
    assert any("webp" in m.get("text", "") for m in body["messages"])
    assert any(".webp" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_valid_image_url_passes_through_unchanged() -> None:
    """Regression guard: adding validation mustn't alter the happy path."""
    captured: list[httpx.Request] = []
    adapter = LineAdapter(transport=_ok_transport(captured))

    await adapter.send(_outbound(
        text="",
        attachments=(
            OutboundAttachment(
                kind="image",
                url="https://cdn.example.com/a.png",
                mime_type="image/png",
            ),
        ),
    ))

    body = json.loads(captured[0].content.decode())
    assert body["messages"] == [
        {
            "type": "image",
            "originalContentUrl": "https://cdn.example.com/a.png",
            "previewImageUrl": "https://cdn.example.com/a.png",
        },
    ]
