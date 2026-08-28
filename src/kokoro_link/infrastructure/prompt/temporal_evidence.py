"""Time anchors for the quality gate's ``temporal_inconsistency`` axis.

One question, asked in one place: *when is now, and when did the things
this message answers actually happen?* Surfaces hand over labelled
instants; this module renders them into the 時間座標 block the judge
reads, so no surface learns how a time is spelled and every surface
spells it the same way.

Deliberately deterministic and judgement-free (LLM-first, D6): nothing
here decides that sixteen hours is too long. It states the elapsed time
as fact and leaves "would a real person say this now?" to the judge —
the rubric explicitly forbids reading a threshold into these numbers.

The 2026-08-27 incident this exists for: the player said 「要回家了」
yesterday afternoon and the character asked 「回家了嗎？」 the next
morning. Every layer held the timestamps; none of them ever reached the
judge, so no axis could see it.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, tzinfo

from kokoro_link.infrastructure.prompt.timing_utils import (
    format_event_time_anchor,
    format_local_current_time,
)

_MAX_LABEL_CHARS = 40
_MAX_DETAIL_CHARS = 80

TemporalEvent = tuple[str, datetime | None]
"""``(label, when)`` — e.g. ``("玩家說要回家", <datetime>)``. A ``None``
instant is dropped rather than rendered undated: an anchor nobody can
place is worse than no anchor, because the axis fires off this block."""


def render_temporal_context_lines(
    *,
    now: datetime | None,
    local_tz: tzinfo,
    events: Iterable[TemporalEvent] = (),
) -> tuple[str, ...]:
    """Build the 時間座標 block: the current clock, then each dated event.

    Returns ``()`` when ``now`` is unknown — without a present moment
    every elapsed reading is meaningless, and the rubric pins the axis
    false on an empty block, which is the correct failure mode. Callers
    can therefore pass whatever they hold and never guard.
    """
    if now is None:
        return ()
    lines = [f"現在：{format_local_current_time(now, local_tz)}"]
    lines.extend(
        line for line in (
            _render_event(label, when, now=now, local_tz=local_tz)
            for label, when in events
        ) if line
    )
    return tuple(lines)


def _render_event(
    label: str,
    when: datetime | None,
    *,
    now: datetime,
    local_tz: tzinfo,
) -> str:
    # Civil day on: the axis this feeds is about staleness, and duration
    # alone hides the boundary that decides it — 「昨天下午」 read the next
    # morning is only "16 小時" but is a different day to both people.
    anchor = format_event_time_anchor(
        when, now, local_tz=local_tz, include_civil_day=True,
    )
    if not anchor:
        return ""
    clean = " ".join((label or "").split())[:_MAX_LABEL_CHARS]
    return f"{clean or '相關素材'}：{anchor}"


def quoted_event(label: str, text: str, when: datetime | None) -> TemporalEvent:
    """A dated event that also carries *what was said*, clipped.

    The judge needs the quote to tell 「玩家說要回家」 (a concern that
    expires) from 「玩家說下週要搬家」 (one that does not); the label
    alone flattens both into "the player said something".
    """
    detail = " ".join((text or "").split())[:_MAX_DETAIL_CHARS]
    return (f"{label}「{detail}」" if detail else label, when)


__all__ = [
    "TemporalEvent",
    "quoted_event",
    "render_temporal_context_lines",
]
