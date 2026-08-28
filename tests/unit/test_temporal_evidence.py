"""TC — the 時間座標 block behind ``temporal_inconsistency``.

The axis is only ever as good as the anchors it is shown, and the block
has one property the rest of the gate context does not: *emptiness is
load-bearing*. The rubric pins the axis false on 「（無）」, which is what
makes the axis safe to ship without wiring every surface at once — so
the empty cases below are the contract, not edge-case tidying.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from kokoro_link.infrastructure.prompt.temporal_evidence import (
    quoted_event,
    render_temporal_context_lines,
)
from kokoro_link.infrastructure.prompt.timing_utils import (
    format_event_time_anchor,
)

TPE = ZoneInfo("Asia/Taipei")


def _now() -> datetime:
    return datetime(2026, 8, 27, 9, 12, tzinfo=TPE)


def test_now_is_always_the_first_line() -> None:
    lines = render_temporal_context_lines(now=_now(), local_tz=TPE)

    assert len(lines) == 1
    assert lines[0].startswith("現在：2026-08-27 09:12")


def test_the_incident_renders_both_the_duration_and_the_day_boundary() -> None:
    """The 2026-08-27 defect, as the judge would now see it.

    Sixteen hours reads as a longish gap; 「1 天前」 is what makes
    「回家了嗎？」 absurd rather than merely late, and duration alone
    hides it — which is why the civil-day track is on for this block.
    """
    lines = render_temporal_context_lines(
        now=_now(),
        local_tz=TPE,
        events=[(
            "玩家最後一次說話",
            datetime(2026, 8, 26, 17, 30, tzinfo=TPE),
        )],
    )

    assert "約 16 小時前（1 天前）" in lines[1]
    assert "2026-08-26 17:30" in lines[1]


def test_a_same_day_gap_carries_no_day_tag() -> None:
    """The tag appears only when calendar and duration disagree —
    otherwise every line would carry a redundant 「今天」."""
    lines = render_temporal_context_lines(
        now=_now(),
        local_tz=TPE,
        events=[("玩家最後一次說話", _now() - timedelta(minutes=10))],
    )

    assert "約 10 分鐘前" in lines[1]
    assert "天前" not in lines[1]


def test_no_now_renders_no_block_at_all() -> None:
    """Without a present moment every elapsed reading is meaningless, and
    an empty block is exactly what pins the axis false."""
    assert render_temporal_context_lines(now=None, local_tz=TPE) == ()


def test_an_undated_event_is_dropped_rather_than_rendered_undated() -> None:
    """An anchor nobody can place is worse than no anchor: the axis fires
    off this block, so a line reading 「玩家說要回家：」 with no time would
    invite exactly the guessing the rubric forbids."""
    lines = render_temporal_context_lines(
        now=_now(),
        local_tz=TPE,
        events=[("玩家最後一次說話", None), ("有時間的事", _now())],
    )

    assert len(lines) == 2
    assert "玩家最後一次說話" not in "\n".join(lines)


def test_quoted_event_carries_what_was_said() -> None:
    """「玩家說要回家」 expires; 「玩家說下週要搬家」 does not. The label
    alone flattens both into "the player said something"."""
    label, when = quoted_event("玩家最後一次說話", "我要回家了", _now())

    assert label == "玩家最後一次說話「我要回家了」"
    assert when == _now()


def test_quoted_event_without_text_degrades_to_the_bare_label() -> None:
    label, _ = quoted_event("玩家最後一次說話", "   ", _now())

    assert label == "玩家最後一次說話"


def test_quoted_event_clips_long_quotes() -> None:
    label, _ = quoted_event("玩家說", "台" * 200, _now())

    assert len(label) < 120


def test_naive_stamps_are_read_as_utc_not_as_local() -> None:
    """The persistence convention. Misreading a naive UTC stamp as Taipei
    local would shift every anchor by eight hours — in the direction that
    makes a stale message look fresh."""
    naive_utc = datetime(2026, 8, 26, 9, 30)  # 17:30 Taipei

    lines = render_temporal_context_lines(
        now=_now(), local_tz=TPE, events=[("玩家說", naive_utc)],
    )

    assert "2026-08-26 17:30" in lines[1]
    assert "約 16 小時前" in lines[1]


def test_a_future_stamp_clamps_instead_of_rendering_negative_time() -> None:
    """Worker/API clock skew must not produce 「約 -3 分鐘前」."""
    lines = render_temporal_context_lines(
        now=_now(),
        local_tz=TPE,
        events=[("時鐘偏移", _now() + timedelta(minutes=3))],
    )

    assert "-" not in lines[1].split("｜", 1)[1]


def test_the_shipped_transcript_anchor_is_unchanged_by_default() -> None:
    """``render_dialogue_line`` (TD, 2026-08-26) must keep rendering
    byte-identically: the civil-day track is opt-in precisely so adding it
    here does not silently redefine a shipped prompt's output."""
    past = datetime(2026, 8, 26, 17, 30, tzinfo=TPE)

    default = format_event_time_anchor(past, _now(), local_tz=TPE)
    opted_in = format_event_time_anchor(
        past, _now(), local_tz=TPE, include_civil_day=True,
    )

    assert default == "2026-08-26 17:30（下午）｜約 16 小時前"
    assert opted_in == "2026-08-26 17:30（下午）｜約 16 小時前（1 天前）"


def test_anchor_helpers_tolerate_a_missing_instant() -> None:
    assert format_event_time_anchor(None, _now(), local_tz=TPE) == ""
    assert format_event_time_anchor(
        _now(), None, local_tz=TPE, include_civil_day=True,
    ) == ""


def test_utc_deployments_still_get_a_block() -> None:
    """``local_tz`` defaults to UTC on containers with no operator zone;
    the block must still render rather than degrade to empty."""
    now = datetime(2026, 8, 27, 1, 12, tzinfo=timezone.utc)

    lines = render_temporal_context_lines(
        now=now,
        local_tz=timezone.utc,
        events=[("玩家說", now - timedelta(hours=16))],
    )

    assert len(lines) == 2
    assert "1 天前" in lines[1]
