"""Minimal structural filtering for the public showcase.

The owner operates the selected character with public posting in mind, so
provenance and planner privacy metadata are advisory rather than publication
gates. A post is rejected only when the pipeline cannot render it: unresolved
schedule provenance, empty text, or no image.

The second job remains a strict data boundary: the candidate shape below is a
whitelist, not a redaction pass — ``image_prompt`` / ``video_prompt``
/ ``reactions_seen_at`` / ``reactions`` / ``source.ref_id`` have no
field to land in, so a future FeedPost column cannot leak by default.

Comments are excluded by *never being read* — this module only ever sees
``FeedPost`` rows, and the loader never asks the comment repository for
anything. That is stronger than filtering them out here.

Everything is pure over domain entities so the rules are unit testable without
a database.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from kokoro_link.domain.entities.feed_post import FeedPost
from kokoro_link.domain.entities.schedule import DailySchedule, ScheduleActivity
from kokoro_link.domain.value_objects.feed_source import SOURCE_SCHEDULE

REASON_SCHEDULE_UNRESOLVED = "schedule:unresolved"
"""A ``schedule``-sourced post whose activity could not be looked up.

This is missing source data, not a privacy verdict. The collect response keeps
the reason visible so regenerated or pruned schedule rows are diagnosable."""


@dataclass(frozen=True, slots=True)
class ShowcaseCandidate:
    """One feed post as far as the public pipeline is concerned.

    This is the whitelist: five fields, all of them things the character
    published about her own life. ``image_url`` stays exactly as stored
    (server-relative ``/v1/public/...``); absolutisation is a publish-time
    concern that needs the deployment's base URL.
    """

    id: str
    kind: str
    created_at: datetime
    text: str
    image_url: str | None = None


@dataclass(frozen=True, slots=True)
class FilterOutcome:
    """Result of :func:`mechanical_filter` — kept candidates plus a
    per-reason tally the console shows verbatim (a filter that silently
    drops everything looks identical to an empty account)."""

    candidates: tuple[ShowcaseCandidate, ...]
    excluded: Mapping[str, int]

    @property
    def excluded_total(self) -> int:
        return sum(self.excluded.values())


def mechanical_filter(
    posts: Iterable[FeedPost],
    *,
    activities: Mapping[str, ScheduleActivity] | None = None,
) -> FilterOutcome:
    """Project ``posts`` onto :class:`ShowcaseCandidate`, dropping the
    structurally unfit. Order is preserved (the repository already returns
    reverse-chronological).

    ``activities`` is the ``{activity_id: ScheduleActivity}`` index used to
    resolve ``FeedSource.schedule(ref_id)`` provenance. Omitting it (or
    passing an index that does not contain a post's ref) rejects every
    schedule-sourced post — see :data:`REASON_SCHEDULE_UNRESOLVED`. The
    service builds the index from the days the candidate posts were written
    on; a caller with no schedule access gets the fail-closed answer rather
    than an unchecked pass.
    """
    index: Mapping[str, ScheduleActivity] = activities or {}
    kept: list[ShowcaseCandidate] = []
    excluded: Counter[str] = Counter()
    for post in posts:
        reason = _rejection_reason(post, index)
        if reason is not None:
            excluded[reason] += 1
            continue
        kept.append(
            ShowcaseCandidate(
                id=post.id,
                kind=post.kind.value,
                created_at=post.created_at,
                text=post.content_text,
                image_url=post.image_url,
            ),
        )
    return FilterOutcome(candidates=tuple(kept), excluded=dict(excluded))


def _rejection_reason(
    post: FeedPost, activities: Mapping[str, ScheduleActivity],
) -> str | None:
    if post.source.kind == SOURCE_SCHEDULE:
        schedule_reason = _schedule_rejection_reason(
            post.source.ref_id, activities,
        )
        if schedule_reason is not None:
            return schedule_reason
    if not post.content_text.strip():
        return "empty_text"
    if not (post.image_url or "").strip():
        return "missing_image"
    return None


def _schedule_rejection_reason(
    ref_id: str | None, activities: Mapping[str, ScheduleActivity],
) -> str | None:
    """Reject only when the post points at source data that no longer exists."""
    activity = activities.get(ref_id) if ref_id else None
    if activity is None:
        return REASON_SCHEDULE_UNRESOLVED
    return None


@dataclass(frozen=True, slots=True)
class PublicActivity:
    """One schedule block as rendered on the public strip.

    ``text`` is the activity ``description`` only — ``location`` is
    deliberately dropped as a hard allowlist boundary. LLM review is advisory
    and cannot substitute for this structural exclusion.

    ``time`` and ``end`` are the block's local wall-clock bounds and the
    interval is **end-exclusive**: a client picks the block in progress
    with ``time <= clock < end``. ``live`` answers the same question, but
    only for the instant the snapshot was built — a document that is
    hours old has a stale flag, which is why the bounds ship too. Without
    ``end`` a client can only see where blocks *start*, so it has no way
    to tell "she is still out" from "that finished three hours ago" and
    an idle gap or a finished day renders as the last block still running.

    ``end`` is ``"24:00"`` for a block that runs to the end of the civil
    day — see :func:`_end_label` for why that spelling is load-bearing.
    """

    time: str
    text: str
    end: str
    live: bool = False


def select_public_activities(
    schedule: DailySchedule | None,
    *,
    now: datetime,
    local_tz,  # noqa: ANN001 — tzinfo; kept loose like ScheduleService
    limit: int = 12,
) -> tuple[PublicActivity | None, tuple[PublicActivity, ...]]:
    """Return every schedule block and the current one for the public surface.

    Planner privacy and participant metadata do not act as publication gates.
    """
    if schedule is None:
        return None, ()
    visible = schedule.activities
    strip = tuple(
        _render(activity, now=now, local_tz=local_tz)
        for activity in sorted(visible, key=lambda item: item.start_at)[:limit]
    )
    current = schedule.activity_at(now)
    now_entry = _render(current, now=now, local_tz=local_tz) if current else None
    return now_entry, strip


def _render(
    activity: ScheduleActivity,
    *,
    now: datetime,
    local_tz,  # noqa: ANN001 — tzinfo
) -> PublicActivity:
    start_local = activity.start_at.astimezone(local_tz)
    end_local = activity.end_at.astimezone(local_tz)
    return PublicActivity(
        time=start_local.strftime("%H:%M"),
        text=activity.description,
        end=_end_label(start_local, end_local),
        live=activity.contains(now),
    )


def _end_label(start_local: datetime, end_local: datetime) -> str:
    """Local ``HH:MM`` for a block's exclusive end, with day end as ``"24:00"``.

    ``strftime`` alone is wrong at exactly one boundary, and it is a
    boundary the planner *manufactures*: every activity is clamped into
    ``[00:00, 24:00)`` of its civil day
    (:func:`~kokoro_link.infrastructure.schedule.llm_planner._build_activities`
    does ``end = min(end, day_end)``, and ``"24:00"`` in planner output is
    parsed to the same instant), so a block running to day end stores an
    ``end_at`` of *next* day midnight. Formatted naively that is
    ``"00:00"`` — a value that sorts **below** its own start, so the
    consumer's ``time <= clock < end`` never holds and the last block of
    the day is permanently invisible instead of permanently current.

    Any end landing on a later civil date than the start collapses to
    ``"24:00"``, not just the clamped-to-midnight case: a block the clamp
    did not reach still runs past the end of the strip's day, and the only
    honest thing a same-day wall-clock interval can say about that is "to
    the end of the day". Reporting its real ``"01:30"`` would wrap the
    interval and empty it, which is the same failure as ``"00:00"``.
    """
    if end_local.date() > start_local.date():
        return "24:00"
    return end_local.strftime("%H:%M")


def dedupe_texts(values: Sequence[str]) -> tuple[str, ...]:
    """Order-preserving unique non-empty strings.

    Used by the publish path so a schedule with three "工作" blocks costs
    one translation call, not three."""
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        trimmed = (value or "").strip()
        if not trimmed or trimmed in seen:
            continue
        seen.add(trimmed)
        out.append(trimmed)
    return tuple(out)
