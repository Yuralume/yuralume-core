"""Unit tests for :class:`CharacterDisposition`.

Pure-function tests — no fixtures, no I/O. Covers the band normalisation
contract, the default sentinel, and ``to_prompt_lines`` empty-on-default
behaviour that the chat / proactive prompt builders rely on.
"""

import pytest

from kokoro_link.domain.value_objects.disposition import CharacterDisposition


def test_default_is_all_medium() -> None:
    disposition = CharacterDisposition()
    assert disposition.self_centeredness == "medium"
    assert disposition.candor == "medium"
    assert disposition.sharing_drive == "medium"
    assert disposition.associativeness == "medium"
    assert disposition.is_default is True


def test_default_to_prompt_lines_is_empty() -> None:
    # The "全 medium 不渲染" contract — chat prompt builder embeds
    # ``*disposition.to_prompt_lines()`` directly, so empty means
    # zero-line contribution.
    assert CharacterDisposition().to_prompt_lines() == []


@pytest.mark.parametrize(
    "field",
    ["self_centeredness", "candor", "sharing_drive", "associativeness"],
)
@pytest.mark.parametrize("band", ["low", "medium", "high"])
def test_each_field_accepts_three_bands(field: str, band: str) -> None:
    disposition = CharacterDisposition(**{field: band})
    assert getattr(disposition, field) == band


def test_blank_string_normalises_to_medium() -> None:
    # API payloads often arrive with "" for unspecified fields — those
    # should be treated as "not set" rather than blowing up.
    disposition = CharacterDisposition(self_centeredness="")
    assert disposition.self_centeredness == "medium"


def test_none_normalises_to_medium() -> None:
    disposition = CharacterDisposition(candor=None)  # type: ignore[arg-type]
    assert disposition.candor == "medium"


def test_unknown_band_raises_valueerror() -> None:
    with pytest.raises(ValueError):
        CharacterDisposition(self_centeredness="extreme")


def test_case_insensitive_and_trimmed() -> None:
    disposition = CharacterDisposition(
        self_centeredness="  HIGH ",
        candor="Low",
    )
    assert disposition.self_centeredness == "high"
    assert disposition.candor == "low"


def test_is_default_false_when_any_field_diverges() -> None:
    assert CharacterDisposition(sharing_drive="high").is_default is False


def test_to_prompt_lines_emits_all_four_when_any_diverges() -> None:
    lines = CharacterDisposition(sharing_drive="high").to_prompt_lines()
    # Header + 4 bullet lines — caller (prompt builder) relies on this
    # shape to avoid a "partial" disposition section that might confuse
    # the LLM about whether unmentioned dimensions are medium or unknown.
    assert len(lines) == 5
    joined = "\n".join(lines)
    assert "自我表達" in joined
    assert "面對歧見" in joined
    assert "分享慾" in joined
    assert "回憶連結" in joined


def test_sharing_drive_low_and_high_describe_message_burst_tendency() -> None:
    low_lines = "\n".join(
        CharacterDisposition(sharing_drive="low").to_prompt_lines(),
    )
    high_lines = "\n".join(
        CharacterDisposition(sharing_drive="high").to_prompt_lines(),
    )

    assert "一兩則短訊" in low_lines
    assert "不會連珠炮洗版" in low_lines
    assert "連珠炮一樣連發幾則" in high_lines


def test_from_payload_none_returns_default() -> None:
    assert CharacterDisposition.from_payload(None).is_default is True
    assert CharacterDisposition.from_payload({}).is_default is True


def test_from_payload_unknown_keys_are_ignored() -> None:
    # Forward compatibility: an older binary reading a newer DB row with
    # extra dimensions shouldn't blow up — just ignore the unknown keys.
    disposition = CharacterDisposition.from_payload(
        {"self_centeredness": "high", "unknown_dim": "extreme"},
    )
    assert disposition.self_centeredness == "high"
    assert disposition.is_default is False


def test_to_payload_round_trips() -> None:
    original = CharacterDisposition(
        self_centeredness="high",
        candor="low",
        sharing_drive="medium",
        associativeness="high",
    )
    restored = CharacterDisposition.from_payload(original.to_payload())
    assert restored == original


def test_with_overrides_is_immutable_replace() -> None:
    base = CharacterDisposition()
    bumped = base.with_overrides(candor="high")
    assert base.candor == "medium"
    assert bumped.candor == "high"
    # Same identity guarantee as the dataclass frozen=True contract —
    # ``with_overrides`` returns a fresh instance, doesn't mutate.
    assert base is not bumped


def test_default_singleton_is_default() -> None:
    assert CharacterDisposition.DEFAULT.is_default is True
