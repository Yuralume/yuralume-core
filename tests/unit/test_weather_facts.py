"""Unit tests for :class:`WeatherFacts` and the WMO code mapping.

Pure functions — no HTTP, no fixtures. Verifies:

* The all-empty case renders empty string (caller spliced unconditionally).
* Partial data fills in only the lines it has (renderer suppresses
  missing signals rather than emitting "高溫 None°C").
* WMO codes get their canonical Chinese phrase; unknown codes degrade
  gracefully without dropping the block.
* Precipitation ≥ 60% triggers the "可能會用到傘" hint sentence.
"""

import pytest

from kokoro_link.infrastructure.weather.facts import (
    WeatherFacts,
    condition_phrase,
)


def test_empty_facts_renders_empty_string() -> None:
    facts = WeatherFacts(location_label="台北")
    assert facts.has_any_signal is False
    assert facts.to_prompt_block() == ""


def test_temperature_only_renders_minimal_block() -> None:
    facts = WeatherFacts(
        location_label="台北",
        condition_code=2,
        temperature_c=23.4,
    )
    block = facts.to_prompt_block()
    assert "台北" in block
    assert "局部多雲" in block
    assert "23.4°C" in block
    # No high / low / precipitation lines when those signals are absent —
    # the renderer must not emit "高溫 None°C" or similar.
    assert "高溫" not in block
    assert "降雨機率" not in block


def test_full_payload_emits_all_sections() -> None:
    facts = WeatherFacts(
        location_label="台北",
        condition_code=61,
        temperature_c=21.2,
        high_c=24.0,
        low_c=20.5,
        precipitation_probability=75,
        is_day=True,
    )
    block = facts.to_prompt_block()
    assert "小雨" in block
    assert "21.2°C" in block
    assert "高溫 24.0°C" in block
    assert "低溫 20.5°C" in block
    assert "75%" in block
    # 75% crosses the umbrella reminder threshold.
    assert "提醒：今日有相當機率會下雨" in block
    assert "白天時段" in block


def test_low_precipitation_skips_umbrella_reminder() -> None:
    facts = WeatherFacts(
        location_label="台北",
        condition_code=1,
        precipitation_probability=20,
    )
    block = facts.to_prompt_block()
    assert "20%" in block
    assert "可能會下雨" not in block


def test_night_is_day_label() -> None:
    facts = WeatherFacts(
        location_label="台北", condition_code=0, is_day=False,
    )
    block = facts.to_prompt_block()
    assert "夜間時段" in block


def test_unknown_wmo_code_falls_back_gracefully() -> None:
    facts = WeatherFacts(
        location_label="台北", condition_code=99999, temperature_c=18.0,
    )
    block = facts.to_prompt_block()
    # Unknown code → generic phrase, but temperature line still appears.
    assert "天氣狀況不明" in block
    assert "18.0°C" in block


@pytest.mark.parametrize(
    ("code", "phrase"),
    [
        (0, "晴朗"),
        (3, "陰天"),
        (61, "小雨"),
        (63, "中雨"),
        (65, "大雨"),
        (95, "雷雨"),
        (None, "天氣狀況不明"),
    ],
)
def test_condition_phrase_table(code: int | None, phrase: str) -> None:
    assert condition_phrase(code) == phrase


def test_high_only_renders_partial() -> None:
    facts = WeatherFacts(location_label="台北", high_c=26.0)
    block = facts.to_prompt_block()
    assert "今日高溫：26.0°C" in block
    assert "低溫" not in block
