"""World-event prompt lines — candidate and recall rendering.

Both chat blocks (events the character has come across but not used, and
events it already brought up with the player) render through one module,
so a fact present in one is present in the other. The link is the fact
that matters most here: a 240-char RSS blurb cannot answer a follow-up
question, and the model cannot fetch a URL it was never shown.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from kokoro_link.application.services.world_event_prompt_lines import (
    MAX_URL_CHARS,
    render_event_candidate_line,
    render_event_recall_line,
)
from kokoro_link.domain.entities.world_event import WorldEvent

UTC = timezone.utc
_NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)


def _event(
    *,
    title: str = "板上在吵新開的拉麵店",
    summary: str = "原文提到排隊三小時，湯頭評價兩極。",
    url: str = "https://www.ptt.cc/bbs/Food/M.1234567890.A.123.html",
    source: str = "PTT 熱門看板",
    locale: str | None = "zh-TW",
) -> WorldEvent:
    return WorldEvent(
        id="we-1",
        source=source,
        title=title,
        summary=summary,
        url=url,
        published_at=_NOW - timedelta(hours=6),
        fetched_at=_NOW - timedelta(hours=5),
        locale=locale,
    )


class TestCandidateLine:
    def test_carries_link_so_a_follow_up_can_be_answered(self) -> None:
        line = render_event_candidate_line(_event())
        assert line is not None
        assert "標題：板上在吵新開的拉麵店" in line
        assert "來源：PTT 熱門看板" in line
        assert "來源地區：zh-TW" in line
        assert "摘要：原文提到排隊三小時" in line
        assert (
            "連結：https://www.ptt.cc/bbs/Food/M.1234567890.A.123.html" in line
        )

    def test_untitled_event_is_dropped(self) -> None:
        assert render_event_candidate_line(_event(title="   ")) is None

    def test_optional_facts_are_omitted_not_faked(self) -> None:
        line = render_event_candidate_line(
            _event(summary="", locale=None, source=""),
        )
        assert line is not None
        assert "摘要：" not in line
        assert "來源地區：" not in line
        assert "來源：" not in line
        assert "連結：" in line

    def test_summary_is_clipped(self) -> None:
        line = render_event_candidate_line(_event(summary="長" * 400))
        assert line is not None
        assert "長" * 400 not in line
        assert "…" in line

    def test_absurdly_long_url_is_dropped_whole_not_truncated(self) -> None:
        # A truncated URL is worse than no URL: it cannot be fetched and
        # it invites the model to present a broken link as a source.
        long_url = "https://example.com/" + "a" * MAX_URL_CHARS
        line = render_event_candidate_line(_event(url=long_url))
        assert line is not None
        assert "連結：" not in line
        assert "標題：" in line

    def test_non_http_url_is_dropped(self) -> None:
        line = render_event_candidate_line(_event(url="javascript:alert(1)"))
        assert line is not None
        assert "連結：" not in line


class TestRecallLine:
    def test_states_when_it_was_brought_up(self) -> None:
        line = render_event_recall_line(
            _event(), mentioned_at=_NOW - timedelta(hours=3), now=_NOW,
        )
        assert line is not None
        assert "3 小時前" in line
        assert "標題：板上在吵新開的拉麵店" in line
        assert "連結：" in line

    def test_recent_mention_reads_as_just_now(self) -> None:
        line = render_event_recall_line(
            _event(), mentioned_at=_NOW - timedelta(minutes=4), now=_NOW,
        )
        assert line is not None
        assert "剛剛" in line

    def test_old_mention_reads_in_days(self) -> None:
        line = render_event_recall_line(
            _event(), mentioned_at=_NOW - timedelta(days=2, hours=1), now=_NOW,
        )
        assert line is not None
        assert "2 天前" in line

    def test_clock_skew_does_not_render_negative_time(self) -> None:
        line = render_event_recall_line(
            _event(), mentioned_at=_NOW + timedelta(minutes=30), now=_NOW,
        )
        assert line is not None
        assert "-" not in line.split("標題：")[0]
        assert "剛剛" in line

    def test_naive_timestamps_are_treated_as_utc(self) -> None:
        line = render_event_recall_line(
            _event(),
            mentioned_at=(_NOW - timedelta(hours=2)).replace(tzinfo=None),
            now=_NOW,
        )
        assert line is not None
        assert "2 小時前" in line

    def test_untitled_event_is_dropped(self) -> None:
        assert (
            render_event_recall_line(
                _event(title=""), mentioned_at=_NOW, now=_NOW,
            )
            is None
        )
