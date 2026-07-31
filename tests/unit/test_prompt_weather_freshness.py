"""Unit tests for the shared weather freshness prompt fragment.

The fragment is spliced into chat, proactive decider and intention judge
prompts, so the invariants asserted here (empty → no-op, fact verbatim,
authority directive always attached) are what stops the three surfaces
from drifting apart on "放晴後還在叫人帶傘".
"""

from __future__ import annotations

from kokoro_link.infrastructure.prompt.weather_freshness import (
    render_weather_fact_lines,
)


def test_blank_weather_renders_nothing() -> None:
    assert render_weather_fact_lines("") == []
    assert render_weather_fact_lines("   \n  ") == []


def test_fact_is_followed_by_freshness_authority() -> None:
    lines = render_weather_fact_lines(
        "台北目前天氣（事實層）：\n- 現在：晴朗，氣溫 26°C",
    )
    assert len(lines) == 2
    assert lines[0] == "台北目前天氣（事實層）：\n- 現在：晴朗，氣溫 26°C"
    assert "以此刻天氣事實為準" in lines[1]
    assert "不要延續已經過時的天氣狀態" in lines[1]


def test_max_fact_chars_clamps_only_the_fact() -> None:
    """Proactive prompts clamp the fact to keep the prompt bounded; the
    directive must survive the clamp or the whole point is lost."""
    lines = render_weather_fact_lines("晴" * 900, max_fact_chars=600)
    assert lines[0] == "晴" * 600
    assert "以此刻天氣事實為準" in lines[1]
