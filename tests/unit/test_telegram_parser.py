from datetime import datetime, timezone

from kokoro_link.domain.value_objects.platform import Platform
from kokoro_link.infrastructure.messaging.telegram.parser import parse_update


def _base_update(**message_overrides: object) -> dict:
    message = {
        "message_id": 77,
        "from": {"id": 1001, "first_name": "U"},
        "chat": {"id": 2002, "type": "private"},
        "date": 1_700_000_000,
        "text": "hello",
    }
    message.update(message_overrides)
    return {"update_id": 5, "message": message}


def test_parses_text_message() -> None:
    result = parse_update(_base_update())

    assert result is not None
    assert result.platform == Platform.TELEGRAM
    assert result.chat_ref == "2002"
    assert result.sender_ref == "1001"
    assert result.text == "hello"
    assert result.platform_message_id == "2002:77"
    assert result.received_at == datetime.fromtimestamp(
        1_700_000_000, tz=timezone.utc,
    )


def test_photo_message_folds_to_placeholder() -> None:
    update = _base_update()
    del update["message"]["text"]
    update["message"]["photo"] = [{"file_id": "x"}]

    result = parse_update(update)
    assert result is not None
    assert result.text == "[使用者傳來一張圖片]"


def test_photo_message_with_caption_includes_caption() -> None:
    update = _base_update()
    del update["message"]["text"]
    update["message"]["photo"] = [{"file_id": "x"}]
    update["message"]["caption"] = "今天的晚餐～"

    result = parse_update(update)
    assert result is not None
    assert result.text == "[使用者傳來一張圖片] 今天的晚餐～"


def test_sticker_message_returns_none() -> None:
    update = _base_update()
    del update["message"]["text"]
    update["message"]["sticker"] = {"file_id": "s"}

    assert parse_update(update) is None


def test_edited_message_ignored() -> None:
    update = {
        "update_id": 5,
        "edited_message": {
            "message_id": 77, "chat": {"id": 2002}, "text": "edited",
        },
    }

    assert parse_update(update) is None


def test_channel_post_without_from_uses_chat_as_sender() -> None:
    update = _base_update()
    del update["message"]["from"]

    result = parse_update(update)

    assert result is not None
    assert result.sender_ref == "2002"


def test_missing_chat_returns_none() -> None:
    update = _base_update()
    del update["message"]["chat"]

    assert parse_update(update) is None


def test_missing_message_id_returns_none() -> None:
    update = _base_update()
    del update["message"]["message_id"]

    assert parse_update(update) is None


def test_empty_text_returns_none() -> None:
    update = _base_update(text="")

    assert parse_update(update) is None


def test_bot_command_start_is_dropped() -> None:
    update = _base_update(
        text="/start",
        entities=[{"type": "bot_command", "offset": 0, "length": 6}],
    )

    assert parse_update(update) is None


def test_bot_command_with_args_is_dropped() -> None:
    update = _base_update(
        text="/help foo",
        entities=[{"type": "bot_command", "offset": 0, "length": 5}],
    )

    assert parse_update(update) is None


def test_bot_command_with_bot_username_is_dropped() -> None:
    update = _base_update(
        text="/start@my_bot",
        entities=[{"type": "bot_command", "offset": 0, "length": 13}],
    )

    assert parse_update(update) is None


def test_slash_in_middle_of_text_is_kept() -> None:
    update = _base_update(
        text="我剛剛輸入了 /start 看看會發生什麼",
        entities=[{"type": "bot_command", "offset": 7, "length": 6}],
    )

    result = parse_update(update)
    assert result is not None
    assert result.text == "我剛剛輸入了 /start 看看會發生什麼"


def test_photo_with_bot_command_caption_is_dropped() -> None:
    update = _base_update()
    del update["message"]["text"]
    update["message"]["photo"] = [{"file_id": "x"}]
    update["message"]["caption"] = "/start"
    update["message"]["caption_entities"] = [
        {"type": "bot_command", "offset": 0, "length": 6},
    ]

    assert parse_update(update) is None
