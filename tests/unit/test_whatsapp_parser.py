from datetime import datetime, timezone

from kokoro_link.domain.value_objects.platform import Platform
from kokoro_link.infrastructure.messaging.whatsapp.parser import parse_whatsapp_event


def test_parse_text_event() -> None:
    parsed = parse_whatsapp_event(
        {
            "id": "msg-1",
            "chat_ref": "12025550123@s.whatsapp.net",
            "sender_ref": "12025550123@s.whatsapp.net",
            "text": "hello",
            "timestamp": "2026-06-03T08:30:00+00:00",
        },
    )

    assert parsed is not None
    assert parsed.platform == Platform.WHATSAPP
    assert parsed.chat_ref == "12025550123@s.whatsapp.net"
    assert parsed.sender_ref == "12025550123@s.whatsapp.net"
    assert parsed.text == "hello"
    assert parsed.platform_message_id == "12025550123@s.whatsapp.net:msg-1"
    assert parsed.received_at == datetime(2026, 6, 3, 8, 30, tzinfo=timezone.utc)


def test_parse_ignores_self_events() -> None:
    assert parse_whatsapp_event(
        {
            "id": "msg-1",
            "chat_ref": "12025550123@s.whatsapp.net",
            "sender_ref": "12025550123@s.whatsapp.net",
            "text": "echo",
            "from_me": True,
        },
    ) is None


def test_parse_media_event_uses_sidecar_urls_as_photo_refs() -> None:
    parsed = parse_whatsapp_event(
        {
            "id": "msg-2",
            "chat_ref": "12025550123@s.whatsapp.net",
            "sender_ref": "12025550123@s.whatsapp.net",
            "media_urls": ["https://asset.test/a.jpg"],
            "timestamp": 1780488000,
        },
    )

    assert parsed is not None
    assert parsed.text
    assert parsed.photo_refs == ("https://asset.test/a.jpg",)
