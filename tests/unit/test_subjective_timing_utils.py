"""Unit tests for ``infrastructure.prompt.timing_utils`` (§4.4)."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from kokoro_link.infrastructure.prompt.timing_utils import (
    describe_idle_natural,
    render_current_time_fact_lines,
    render_subjective_time_topical_hint,
    time_of_day_hint,
)


@pytest.mark.parametrize(
    "minutes, expected_substring",
    [
        (0.5, "剛剛"),
        (5, "5 分鐘前"),
        (45, "45 分鐘前"),
        (180, "3.0 小時前"),
        (60 * 12, "12 小時前"),
        (60 * 36, "久違了"),
        (60 * 24 * 7, "天前"),
    ],
)
def test_describe_idle_natural_returns_natural_language(
    minutes: float, expected_substring: str,
) -> None:
    rendered = describe_idle_natural(minutes)
    assert expected_substring in rendered


def test_topical_hint_empty_when_idle_unknown() -> None:
    assert render_subjective_time_topical_hint(None) == []


@pytest.mark.parametrize("minutes", [0, 30, 60 * 3, 60 * 5.9])
def test_topical_hint_silent_below_threshold(minutes: float) -> None:
    assert render_subjective_time_topical_hint(minutes) == []


@pytest.mark.parametrize("minutes", [60 * 6, 60 * 24, 60 * 24 * 14])
def test_topical_hint_surfaces_above_threshold(minutes: float) -> None:
    lines = render_subjective_time_topical_hint(minutes)
    assert lines, "expected catch-up hint above the 6h threshold"
    joined = "\n".join(lines)
    assert "久未聯絡" in joined
    assert "catch-up" in joined
    assert "話題層" in joined


def test_topical_hint_decouples_from_emotional_layer() -> None:
    """Per HUMANIZATION_ROADMAP §4.4 the hint must self-describe as the
    topical layer so the LLM can keep idle-drift emotional signals
    separate."""
    lines = render_subjective_time_topical_hint(60 * 24)
    joined = "\n".join(lines)
    assert "情緒層" in joined or "idle drift" in joined


def test_current_time_fact_renders_operator_local_clock() -> None:
    now = datetime(2026, 6, 19, 23, 30, tzinfo=timezone.utc)

    lines = render_current_time_fact_lines(now, ZoneInfo("Asia/Taipei"))

    joined = "\n".join(lines)
    assert "使用者本地時區" in joined
    assert "現在時間：2026-06-20 07:30" in joined
    assert "UTC" not in joined
    assert "清晨" in joined


def test_current_time_fact_can_render_without_heading_for_embedded_blocks() -> None:
    now = datetime(2026, 6, 19, 23, 30, tzinfo=timezone.utc)

    lines = render_current_time_fact_lines(
        now,
        ZoneInfo("Asia/Taipei"),
        heading=None,
    )

    assert lines == ["- 現在時間：2026-06-20 07:30 CST（清晨）"]


@pytest.mark.parametrize(
    "hour, expected",
    [
        (3, "深夜"),
        (7, "清晨"),
        (10, "上午"),
        (13, "中午前後"),
        (16, "下午"),
        (20, "晚上"),
        (23, "夜深"),
    ],
)
def test_time_of_day_hint(hour: int, expected: str) -> None:
    assert time_of_day_hint(
        datetime(2026, 6, 20, hour, 0, tzinfo=ZoneInfo("Asia/Taipei")),
    ) == expected
