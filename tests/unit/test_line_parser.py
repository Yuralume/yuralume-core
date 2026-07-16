from datetime import datetime, timezone

from kokoro_link.domain.value_objects.platform import Platform
from kokoro_link.infrastructure.messaging.line.parser import parse_webhook


def _text_event(
    *, source: dict, text: str = "hello", message_id: str = "m1",
    timestamp: int = 1_700_000_000_000,
) -> dict:
    return {
        "type": "message",
        "replyToken": "r-1",
        "source": source,
        "timestamp": timestamp,
        "message": {"type": "text", "id": message_id, "text": text},
    }


def test_parses_user_text_event() -> None:
    payload = {"events": [_text_event(source={"type": "user", "userId": "U123"})]}

    result = parse_webhook(payload)

    assert len(result) == 1
    msg = result[0]
    assert msg.platform == Platform.LINE
    assert msg.chat_ref == "U123"
    assert msg.sender_ref == "U123"
    assert msg.text == "hello"
    assert msg.platform_message_id == "m1"
    assert msg.received_at == datetime.fromtimestamp(
        1_700_000_000, tz=timezone.utc,
    )


def test_group_event_uses_group_id_as_chat_ref() -> None:
    payload = {
        "events": [
            _text_event(source={"type": "group", "groupId": "G1", "userId": "U7"}),
        ],
    }

    result = parse_webhook(payload)

    assert len(result) == 1
    assert result[0].chat_ref == "G1"
    assert result[0].sender_ref == "U7"


def test_room_event_uses_room_id_as_chat_ref() -> None:
    payload = {
        "events": [_text_event(source={"type": "room", "roomId": "R1"})],
    }

    result = parse_webhook(payload)

    assert len(result) == 1
    assert result[0].chat_ref == "R1"
    assert result[0].sender_ref == "R1"  # falls back when userId missing


def test_non_text_message_skipped() -> None:
    payload = {
        "events": [
            {
                "type": "message",
                "source": {"type": "user", "userId": "U1"},
                "message": {"type": "sticker", "id": "m1"},
            },
        ],
    }

    assert parse_webhook(payload) == []


def test_image_message_folds_to_placeholder() -> None:
    payload = {
        "events": [
            {
                "type": "message",
                "source": {"type": "user", "userId": "U1"},
                "message": {"type": "image", "id": "m99"},
                "timestamp": 1_700_000_000_000,
            },
        ],
    }

    result = parse_webhook(payload)
    assert len(result) == 1
    assert result[0].text == "[使用者傳來一張圖片]"
    assert result[0].platform_message_id == "m99"


def test_non_message_events_skipped() -> None:
    payload = {
        "events": [
            {"type": "follow", "source": {"type": "user", "userId": "U1"}},
            {"type": "join", "source": {"type": "group", "groupId": "G"}},
        ],
    }

    assert parse_webhook(payload) == []


def test_mixed_batch_returns_only_text_messages() -> None:
    payload = {
        "events": [
            {"type": "follow", "source": {"type": "user", "userId": "U1"}},
            _text_event(source={"type": "user", "userId": "U1"}, message_id="m1"),
            _text_event(source={"type": "user", "userId": "U2"}, message_id="m2"),
        ],
    }

    result = parse_webhook(payload)

    assert [m.platform_message_id for m in result] == ["m1", "m2"]


def test_missing_events_array_returns_empty() -> None:
    assert parse_webhook({}) == []
    assert parse_webhook({"events": "not-a-list"}) == []


def test_unknown_source_type_skipped() -> None:
    payload = {
        "events": [_text_event(source={"type": "unknown", "id": "?"})],
    }

    assert parse_webhook(payload) == []
