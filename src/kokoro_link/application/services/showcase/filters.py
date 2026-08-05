"""Mechanical filter — the part of the showcase pipeline that has no
opinions and cannot be talked out of anything.

Two independent jobs:

1. **Drop whole posts** whose *source* makes them structurally unfit for
   a public wall. Two families:

   * ``silence`` — those posts exist because the owner went quiet, so
     their subject matter is the owner's interaction cadence, not the
     character's life.
   * ``schedule`` — a post seeded by a schedule block inherits that
     block's privacy. The strip already refuses to render an
     operator-attached or planner-private activity
     (:func:`is_public_activity`); a post narrating the same block
     ("今天跟他一起去吃拉麵") says strictly more about the owner's real
     day than the strip entry it was refused. Same predicate, both
     surfaces — see :func:`mechanical_filter`.

2. **Never copy internal fields out.** The candidate shape below is a
   whitelist, not a redaction pass — ``image_prompt`` / ``video_prompt``
   / ``reactions_seen_at`` / ``reactions`` / ``source.ref_id`` have no
   field to land in, so a future FeedPost column cannot leak by default.

Comments are excluded by *never being read* — this module only ever sees
``FeedPost`` rows, and the loader never asks the comment repository for
anything. That is stronger than filtering them out here.

Everything is pure over domain entities so the red lines are unit
testable without a database.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from kokoro_link.domain.entities.feed_post import FeedPost
from kokoro_link.domain.entities.schedule import (
    OPERATOR_ANY_INVOLVEMENT_ROLES,
    DailySchedule,
    ScenePrivacy,
    ScheduleActivity,
)
from kokoro_link.domain.value_objects.feed_source import (
    SOURCE_SCHEDULE,
    SOURCE_SILENCE,
)

EXCLUDED_SOURCE_KINDS: frozenset[str] = frozenset({SOURCE_SILENCE})
"""Source kinds a showcase snapshot must never carry.

``silence`` is the only member today (``feed_candidates._collect_silence``
composes it from "the operator has not replied in N hours"). Kept as a set
rather than an ``if`` so adding the next owner-shaped source is a one-line
change with an obvious test seam.

Note the import: the module-level ``SOURCE_SILENCE`` constant, **not**
``FeedSource.SILENCE``. The VO is a ``slots=True`` dataclass and its
``ClassVar`` aliases were turned into slot descriptors by the slots
rewrite, so ``FeedSource.SILENCE`` is a ``member_descriptor`` that
compares equal to nothing — a filter written against it silently matches
zero posts."""

REASON_SCHEDULE_UNRESOLVED = "schedule:unresolved"
"""A ``schedule``-sourced post whose activity could not be looked up.

Rejected, not kept: the *only* thing that makes a schedule post
publishable is the activity behind it being public, and an unresolvable
ref is exactly the state in which that cannot be established. Failing
open here would mean the one post family that can narrate the owner's
real day is also the one family that skips the check whenever the day's
schedule row was regenerated or pruned. The collect response carries the
tally per reason, so a run that suddenly rejects a pile of posts is visible
rather than silent."""

REASON_SCHEDULE_NON_PUBLIC = "schedule:non_public"
"""The activity resolved and :func:`is_public_activity` said no —
operator-attached, or planner-marked private/intimate."""

NON_PUBLIC_SCENE_PRIVACY: frozenset[ScenePrivacy] = frozenset(
    {ScenePrivacy.PRIVATE, ScenePrivacy.INTIMATE},
)
"""Planner-declared privacy readings that keep an activity off the public
schedule strip. ``None`` (legacy / planner did not fill it) is **not**
treated as private — it is the common case on older rows, and excluding it
would empty the strip entirely. The compensating control is that the strip
only ever carries ``description``, never ``location``."""


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
                # A video-only post keeps its text and loses its media:
                # the snapshot schema has no video field, and inventing
                # one here would ship an asset nothing renders.
                image_url=post.image_url,
            ),
        )
    return FilterOutcome(candidates=tuple(kept), excluded=dict(excluded))


def _rejection_reason(
    post: FeedPost, activities: Mapping[str, ScheduleActivity],
) -> str | None:
    if post.source.kind in EXCLUDED_SOURCE_KINDS:
        return f"source:{post.source.kind}"
    if post.source.kind == SOURCE_SCHEDULE:
        schedule_reason = _schedule_rejection_reason(
            post.source.ref_id, activities,
        )
        if schedule_reason is not None:
            return schedule_reason
    if not post.content_text.strip():
        return "empty_text"
    return None


def _schedule_rejection_reason(
    ref_id: str | None, activities: Mapping[str, ScheduleActivity],
) -> str | None:
    """Machine verdict on a schedule-sourced post.

    Deliberately *not* deferred to the human review queue: the LLM
    reviewer reads the post text, and "今天跟他一起看了場電影" reads as a
    perfectly ordinary character post. Only the provenance row knows the
    operator was in that block, so the check that has the evidence is the
    one that has to make the call."""
    activity = activities.get(ref_id) if ref_id else None
    if activity is None:
        return REASON_SCHEDULE_UNRESOLVED
    if not is_public_activity(activity):
        return REASON_SCHEDULE_NON_PUBLIC
    return None


@dataclass(frozen=True, slots=True)
class PublicActivity:
    """One schedule block as rendered on the public strip.

    ``text`` is the activity ``description`` only — ``location`` is
    deliberately dropped, because a real venue name is exactly the
    identifying detail the LLM review exists to catch on posts, and the
    schedule strip gets no LLM review (it is regenerated and re-translated
    every run, so a per-item approval loop would never converge).
    """

    time: str
    text: str
    live: bool = False


def select_public_activities(
    schedule: DailySchedule | None,
    *,
    now: datetime,
    local_tz,  # noqa: ANN001 — tzinfo; kept loose like ScheduleService
    limit: int = 12,
) -> tuple[PublicActivity | None, tuple[PublicActivity, ...]]:
    """Return ``(now_entry, strip)`` for the public schedule surface.

    ``now_entry`` is the block in progress, or ``None`` in a gap **or when
    the block in progress was filtered out** — a private block must not
    turn into a suspicious hole *and* must not be described; absent is the
    honest rendering of both.
    """
    if schedule is None:
        return None, ()
    visible = [
        activity for activity in schedule.activities if is_public_activity(activity)
    ]
    strip = tuple(
        _render(activity, now=now, local_tz=local_tz)
        for activity in sorted(visible, key=lambda item: item.start_at)[:limit]
    )
    current = schedule.activity_at(now)
    now_entry = (
        _render(current, now=now, local_tz=local_tz)
        if current is not None and is_public_activity(current)
        else None
    )
    return now_entry, strip


def is_public_activity(activity: ScheduleActivity) -> bool:
    """Whether a schedule block may appear on the public strip.

    Excluded: anything the operator is attached to (live commitment *or*
    its lapsed record — both describe the owner's real-life plans, which
    is precisely what a public wall must not narrate), and anything the
    planner marked private/intimate.
    """
    for ref in activity.participant_refs:
        if ref.actor_kind == "operator":
            return False
        if ref.role in OPERATOR_ANY_INVOLVEMENT_ROLES:
            return False
    return activity.scene_privacy not in NON_PUBLIC_SCENE_PRIVACY


def _render(
    activity: ScheduleActivity,
    *,
    now: datetime,
    local_tz,  # noqa: ANN001 — tzinfo
) -> PublicActivity:
    return PublicActivity(
        time=activity.start_at.astimezone(local_tz).strftime("%H:%M"),
        text=activity.description,
        live=activity.contains(now),
    )


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
