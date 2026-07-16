import pytest

from kokoro_link.application.services.outbound_message_segments import (
    segment_outbound_message,
    send_segmented_outbound,
    split_outbound_text_segments,
    strip_action_narration,
)
from kokoro_link.contracts.messaging import OutboundAttachment, OutboundMessage
from kokoro_link.domain.value_objects.platform import Platform
from kokoro_link.infrastructure.messaging.fake_adapter import FakeChannelAdapter


def _message(**overrides: object) -> OutboundMessage:
    defaults = {
        "platform": Platform.TELEGRAM,
        "chat_ref": "chat-1",
        "text": "第一則\n\n第二則",
        "credentials": {"bot_token": "token"},
        "attachments": (),
    }
    defaults.update(overrides)
    return OutboundMessage(**defaults)  # type: ignore[arg-type]


def test_split_outbound_text_segments_uses_blank_lines() -> None:
    assert split_outbound_text_segments("第一則\n\n第二則\n\n第三則") == (
        "第一則",
        "第二則",
        "第三則",
    )


def test_strip_action_narration_removes_phone_reply_actions() -> None:
    text = "真的好久沒聯絡耶\n\n*把手機相簿往下滑* 我最近在整理照片"

    assert strip_action_narration(text) == "真的好久沒聯絡耶\n\n我最近在整理照片"
    assert split_outbound_text_segments(text) == (
        "真的好久沒聯絡耶",
        "我最近在整理照片",
    )


def test_segment_outbound_message_puts_attachments_on_last_segment() -> None:
    attachment = OutboundAttachment(
        kind="image",
        url="https://cdn.example.test/photo.png",
        mime_type="image/png",
    )

    messages = segment_outbound_message(
        _message(text="第一則\n\n第二則", attachments=(attachment,)),
    )

    assert [message.text for message in messages] == ["第一則", "第二則"]
    assert messages[0].attachments == ()
    assert messages[1].attachments == (attachment,)


def test_segment_outbound_message_keeps_attachment_only_send() -> None:
    attachment = OutboundAttachment(
        kind="image",
        url="https://cdn.example.test/photo.png",
        mime_type="image/png",
    )

    messages = segment_outbound_message(
        _message(text="   ", attachments=(attachment,)),
    )

    assert len(messages) == 1
    assert messages[0].text == ""
    assert messages[0].attachments == (attachment,)


def test_segment_reply_context_rides_first_segment_only() -> None:
    """Reply affinity (e.g. LINE's one-time replyToken) is single-use:
    the first bubble consumes it, the rest must fall back to push."""
    messages = segment_outbound_message(
        _message(
            text="第一則\n\n第二則\n\n第三則",
            reply_context={"reply_token": "r-1"},
        ),
    )

    assert [m.reply_context for m in messages] == [
        {"reply_token": "r-1"},
        {},
        {},
    ]


@pytest.mark.asyncio
async def test_send_segmented_outbound_hands_all_segments_as_one_batch() -> None:
    """The adapter must see the whole logical reply at once so
    batch-capable platforms (LINE) can pack bubbles into fewer calls."""
    adapter = FakeChannelAdapter(Platform.LINE)

    await send_segmented_outbound(adapter, _message(
        platform=Platform.LINE,
        text="第一則\n\n第二則\n\n第三則",
        reply_context={"reply_token": "r-1"},
    ))

    assert len(adapter.batches) == 1
    batch = adapter.batches[0]
    assert [m.text for m in batch] == ["第一則", "第二則", "第三則"]
    assert [m.reply_context for m in batch] == [
        {"reply_token": "r-1"}, {}, {},
    ]
    # Per-bubble view stays intact for adapters without batch support.
    assert [m.text for m in adapter.sent] == ["第一則", "第二則", "第三則"]


@pytest.mark.asyncio
async def test_send_segmented_outbound_empty_message_sends_nothing() -> None:
    adapter = FakeChannelAdapter(Platform.LINE)

    await send_segmented_outbound(
        adapter, _message(platform=Platform.LINE, text="   ", attachments=()),
    )

    assert adapter.batches == []
    assert adapter.sent == []


def test_segment_attachment_only_send_keeps_reply_context() -> None:
    attachment = OutboundAttachment(
        kind="image",
        url="https://cdn.example.test/photo.png",
        mime_type="image/png",
    )

    messages = segment_outbound_message(
        _message(
            text="",
            attachments=(attachment,),
            reply_context={"reply_token": "r-1"},
        ),
    )

    assert len(messages) == 1
    assert messages[0].reply_context == {"reply_token": "r-1"}
