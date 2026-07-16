from datetime import timezone

from kokoro_link.domain.value_objects.platform import Platform
from kokoro_link.infrastructure.messaging.discord.parser import parse_message_create


def test_parse_message_create_normalizes_text_message() -> None:
    parsed = parse_message_create(
        {
            "id": "m1",
            "channel_id": "c1",
            "timestamp": "2026-06-03T01:02:03.000000+00:00",
            "content": "hello",
            "author": {"id": "u1"},
        },
        bot_user_id="bot",
    )

    assert parsed is not None
    assert parsed.platform == Platform.DISCORD
    assert parsed.chat_ref == "c1"
    assert parsed.sender_ref == "u1"
    assert parsed.text == "hello"
    assert parsed.platform_message_id == "c1:m1"
    assert parsed.received_at.tzinfo == timezone.utc


def test_parse_message_create_ignores_bot_and_own_messages() -> None:
    message = {
        "id": "m1",
        "channel_id": "c1",
        "content": "hello",
        "author": {"id": "bot", "bot": True},
    }

    assert parse_message_create(message, bot_user_id="bot") is None


def test_parse_message_create_folds_image_attachment_into_placeholder() -> None:
    parsed = parse_message_create(
        {
            "id": "m1",
            "channel_id": "c1",
            "content": "",
            "author": {"id": "u1"},
            "attachments": [
                {
                    "url": "https://cdn.discordapp.com/a.png",
                    "content_type": "image/png",
                },
                {
                    "url": "https://cdn.discordapp.com/a.txt",
                    "content_type": "text/plain",
                },
            ],
        },
    )

    assert parsed is not None
    assert parsed.text == "[使用者傳來一個附件]"
    assert parsed.photo_refs == ("https://cdn.discordapp.com/a.png",)
