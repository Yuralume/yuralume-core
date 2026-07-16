from kokoro_link.application.services.outbound_message_segments import (
    segment_outbound_message,
    split_outbound_text_segments,
    strip_action_narration,
)
from kokoro_link.contracts.messaging import OutboundAttachment, OutboundMessage
from kokoro_link.domain.value_objects.platform import Platform


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
