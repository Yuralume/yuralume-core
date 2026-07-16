import pytest

from kokoro_link.domain.value_objects.platform import CANONICAL_PLATFORMS, Platform


def test_canonical_constants_match_string_values() -> None:
    assert Platform.TELEGRAM.value == "telegram"
    assert Platform.LINE.value == "line"
    assert Platform.DISCORD.value == "discord"
    assert Platform.WHATSAPP.value == "whatsapp"
    assert set(CANONICAL_PLATFORMS) == {
        Platform.TELEGRAM,
        Platform.LINE,
        Platform.DISCORD,
        Platform.WHATSAPP,
    }


def test_equality_by_value() -> None:
    assert Platform("telegram") == Platform.TELEGRAM
    assert Platform.from_string("LINE") == Platform.LINE


def test_value_is_normalized() -> None:
    assert Platform("  Telegram  ").value == "telegram"


def test_rejects_empty_value() -> None:
    with pytest.raises(ValueError):
        Platform("")
    with pytest.raises(ValueError):
        Platform("   ")
