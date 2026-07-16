"""Daily schedule domain entities.

A ``DailySchedule`` is a character's planned activities for one real-world
date in the character owner's fixed user timezone. It consists of a tuple
of ``ScheduleActivity`` blocks that describe what the character is doing
during a given time range.

Design notes:
- ``category`` is an open-ended free-form string so characters can express
  unique, creative activities ("觀星" / "寫歌詞" / "和貓發呆"). A VO was
  considered but rejected — open strings align with ``MemoryKind`` style
  and give the LLM room to be imaginative.
- Activities are **non-overlapping**: the builder trims overlaps before
  persistence, so ``activity_at`` is unambiguous.
- A gap between activities is simply "idle time" — not every minute of
  the day needs to be filled.
- ``date`` is the civil date in the character's timezone (server TZ for
  Phase 1). ``start_at`` / ``end_at`` remain absolute UTC datetimes so
  boundary comparisons are DST-safe.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from enum import StrEnum
from uuid import uuid4

from kokoro_link.domain.value_objects.actor import ParticipantRef

DEFAULT_UNKNOWN_BUSY_SCORE = 0.4
"""Reachable fallback when an activity has no usable busy score."""

OPERATOR_CONFIRMED_SHARED_ROLE = "operator_confirmed_shared"
OPERATOR_INVITE_PENDING_ROLE = "operator_invite_pending"
OPERATOR_WISH_ROLE = "operator_wish"
OPERATOR_INVOLVEMENT_ROLES = frozenset(
    {
        OPERATOR_CONFIRMED_SHARED_ROLE,
        OPERATOR_INVITE_PENDING_ROLE,
        OPERATOR_WISH_ROLE,
    },
)


class ScenePrivacy(StrEnum):
    PUBLIC = "public"
    SEMI_PUBLIC = "semi_public"
    PRIVATE = "private"
    INTIMATE = "intimate"


class MeetingAffordance(StrEnum):
    OPEN_TO_ENCOUNTER = "open_to_encounter"
    INVITE_ONLY = "invite_only"
    NOT_AVAILABLE = "not_available"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _clamp_busy(value: float) -> float:
    """Clamp a busy score into ``[0.0, 1.0]`` inclusive."""
    if value is None:
        return DEFAULT_UNKNOWN_BUSY_SCORE
    try:
        score = float(value)
    except (TypeError, ValueError):
        return DEFAULT_UNKNOWN_BUSY_SCORE
    return max(0.0, min(1.0, score))


@dataclass(frozen=True, slots=True)
class ScheduleActivity:
    """One block of planned activity within a day.

    ``busy_score`` (0.0–1.0) captures the cost of replying to a message
    during this block — 0 means freely reachable, 1 means effectively
    unable to check the phone. It's used to shape prompt tone and to
    decide whether the busy-defer LLM decider is worth invoking.
    """

    id: str
    start_at: datetime
    end_at: datetime
    description: str
    category: str
    location: str | None = None
    busy_score: float = DEFAULT_UNKNOWN_BUSY_SCORE
    memorialized: bool = False
    has_memory: bool = False
    companion_names: tuple[str, ...] = field(default_factory=tuple)
    """同伴顯示名清單，對應 ``Character.companions`` 中的 NPC（也允許
    為純文字提示中出現的臨時人名）。Schedule planner 在規劃這個時段
    時若安排「跟誰一起」會填入這欄；prompt builder 把它渲染到行程段，
    post-turn extractor 接著把這些名字寫進 ``MemoryItem.participants``
    (``actor_kind="npc"``)，讓記憶帶上對象、不再像獨白。空 tuple = 該
    時段角色獨自進行（預設）。"""
    participant_refs: tuple[ParticipantRef, ...] = field(default_factory=tuple)
    """真實參與者的結構化引用。

    ``companion_names`` 保留給私人 NPC / 舊資料；若活動是和另一個系統
    角色真實互動，使用 ``ParticipantRef(actor_kind="character", ...)`` 寫
    入這欄，讓 UI 與記憶流程能分辨「真實角色」與文字同伴。"""
    scene_privacy: ScenePrivacy | None = None
    """LLM-produced semantic privacy affordance for Scene Access.

    ``None`` means legacy / unknown, not "safe" or "private". Python
    code must not derive this from location keywords; planners or
    SceneAccessJudge may provide it as a structured semantic result."""
    meeting_affordance: MeetingAffordance | None = None
    """LLM-produced indication of whether an activity naturally allows
    encounter, requires invitation, or is not available for meeting."""

    def __post_init__(self) -> None:
        object.__setattr__(self, "busy_score", _clamp_busy(self.busy_score))
        if self.scene_privacy is not None:
            object.__setattr__(
                self,
                "scene_privacy",
                ScenePrivacy(self.scene_privacy),
            )
        if self.meeting_affordance is not None:
            object.__setattr__(
                self,
                "meeting_affordance",
                MeetingAffordance(self.meeting_affordance),
            )

    @classmethod
    def create(
        cls,
        *,
        start_at: datetime,
        end_at: datetime,
        description: str,
        category: str,
        location: str | None = None,
        busy_score: float | None = None,
        memorialized: bool = False,
        has_memory: bool = False,
        companion_names: tuple[str, ...] | list[str] | None = None,
        participant_refs: tuple[ParticipantRef, ...] | list[ParticipantRef] | None = None,
        scene_privacy: ScenePrivacy | str | None = None,
        meeting_affordance: MeetingAffordance | str | None = None,
    ) -> "ScheduleActivity":
        desc = description.strip()
        if not desc:
            raise ValueError("ScheduleActivity description must be non-empty")
        cat = category.strip()
        if not cat:
            raise ValueError("ScheduleActivity category must be non-empty")
        if end_at <= start_at:
            raise ValueError("ScheduleActivity end_at must be after start_at")
        if start_at.tzinfo is None or end_at.tzinfo is None:
            raise ValueError("ScheduleActivity times must be timezone-aware")
        loc = location.strip() if isinstance(location, str) else None
        resolved_busy = (
            _clamp_busy(busy_score)
            if busy_score is not None
            else default_busy_score(cat)
        )
        names: tuple[str, ...] = ()
        if companion_names:
            seen: set[str] = set()
            cleaned: list[str] = []
            for raw in companion_names:
                if not isinstance(raw, str):
                    continue
                trimmed = raw.strip()
                if not trimmed or trimmed in seen:
                    continue
                seen.add(trimmed)
                cleaned.append(trimmed)
            names = tuple(cleaned)
        return cls(
            id=str(uuid4()),
            start_at=start_at.astimezone(timezone.utc),
            end_at=end_at.astimezone(timezone.utc),
            description=desc,
            category=cat,
            location=loc or None,
            busy_score=resolved_busy,
            memorialized=memorialized,
            has_memory=has_memory,
            companion_names=names,
            participant_refs=_dedupe_participant_refs(participant_refs or ()),
            scene_privacy=_coerce_scene_privacy(scene_privacy),
            meeting_affordance=_coerce_meeting_affordance(meeting_affordance),
        )

    def with_memorialized(self, flag: bool = True) -> "ScheduleActivity":
        return replace(self, memorialized=flag)

    def with_memory_state(
        self,
        *,
        memorialized: bool | None = None,
        has_memory: bool | None = None,
    ) -> "ScheduleActivity":
        return replace(
            self,
            memorialized=self.memorialized if memorialized is None else memorialized,
            has_memory=self.has_memory if has_memory is None else has_memory,
        )

    def contains(self, moment: datetime) -> bool:
        instant = (
            moment.astimezone(timezone.utc)
            if moment.tzinfo
            else moment.replace(tzinfo=timezone.utc)
        )
        return self.start_at <= instant < self.end_at

    @property
    def duration(self) -> timedelta:
        return self.end_at - self.start_at


# Keyword-driven default busy scores, used when a planner omits the value.
# The mapping is intentionally shallow — a free-form ``category`` string can
# be anything ("觀星", "寫歌詞", "和貓發呆") so we match on well-known English
# and Chinese stems and fall back to a reachable default for anything novel.
_BUSY_DEFAULTS_EN: dict[str, float] = {
    "sleep": 0.95,
    "rest": 0.1,
    "leisure": 0.2,
    "hobby": 0.3,
    "social": 0.3,
    "meal": 0.2,
    "drive": 0.95,
    "exam": 0.95,
    "interview": 0.95,
    "exercise": 0.65,
    "errand": 0.45,
    "commute": 0.45,
    "work": 0.55,
    "study": 0.6,
    "meeting": 0.8,
    "class": 0.7,
    "deadline": 0.8,
}
_BUSY_DEFAULTS_ZH: dict[str, float] = {
    "睡": 0.95,
    "休息": 0.1,
    "放鬆": 0.2,
    "休閒": 0.2,
    "興趣": 0.3,
    "社交": 0.3,
    "用餐": 0.2,
    "吃飯": 0.2,
    "早餐": 0.2,
    "午餐": 0.2,
    "晚餐": 0.2,
    "開車": 0.95,
    "駕駛": 0.95,
    "考試": 0.95,
    "面試": 0.95,
    "運動": 0.65,
    "採買": 0.45,
    "通勤": 0.45,
    "工作": 0.55,
    "讀書": 0.6,
    "學習": 0.6,
    "會議": 0.8,
    "上課": 0.7,
    "專案": 0.75,
}


def default_busy_score(category: str) -> float:
    """Return a heuristic busy score for ``category``.

    Matches against a small set of English and Chinese stems; anything
    unrecognised returns ``0.4`` so novel creative categories start from
    a reachable-but-not-idle default rather than biasing toward defer.
    """
    if not category:
        return DEFAULT_UNKNOWN_BUSY_SCORE
    lowered = category.strip().lower()
    for stem, score in _BUSY_DEFAULTS_EN.items():
        if stem in lowered:
            return score
    for stem, score in _BUSY_DEFAULTS_ZH.items():
        if stem in category:
            return score
    return DEFAULT_UNKNOWN_BUSY_SCORE


def _coerce_scene_privacy(raw: ScenePrivacy | str | None) -> ScenePrivacy | None:
    if raw is None:
        return None
    try:
        return ScenePrivacy(raw)
    except ValueError:
        return None


def _coerce_meeting_affordance(
    raw: MeetingAffordance | str | None,
) -> MeetingAffordance | None:
    if raw is None:
        return None
    try:
        return MeetingAffordance(raw)
    except ValueError:
        return None


def _dedupe_participant_refs(
    refs: tuple[ParticipantRef, ...] | list[ParticipantRef],
) -> tuple[ParticipantRef, ...]:
    seen: set[tuple[str, str | None, str, str | None]] = set()
    out: list[ParticipantRef] = []
    for ref in refs:
        if not isinstance(ref, ParticipantRef):
            continue
        key = (ref.actor_kind, ref.actor_id, ref.display_name, ref.role)
        if key in seen:
            continue
        seen.add(key)
        out.append(ref)
    return tuple(out)


@dataclass(frozen=True, slots=True)
class DailySchedule:
    """A character's planned day.

    ``is_planned`` distinguishes a fully-planned day (the LLM planner
    ran end-to-end) from a "seed only" day that exists because the
    chat-extracted a future commitment (e.g. "明天 7 點看電影") and
    landed it ahead of plan_day. Without this flag,
    :meth:`ScheduleService.ensure_schedule` would short-circuit on the
    seed-only row and the rest of the day would never get planned.
    """

    id: str
    character_id: str
    date: date
    activities: tuple[ScheduleActivity, ...] = field(default_factory=tuple)
    generated_at: datetime = field(default_factory=_utcnow)
    is_planned: bool = True

    @classmethod
    def create(
        cls,
        *,
        character_id: str,
        date_: date,
        activities: list[ScheduleActivity] | tuple[ScheduleActivity, ...] = (),
        generated_at: datetime | None = None,
        id_: str | None = None,
        is_planned: bool = True,
    ) -> "DailySchedule":
        ordered = tuple(sorted(activities, key=lambda a: a.start_at))
        return cls(
            id=id_ or str(uuid4()),
            character_id=character_id,
            date=date_,
            activities=ordered,
            generated_at=generated_at or _utcnow(),
            is_planned=is_planned,
        )

    def with_is_planned(self, flag: bool = True) -> "DailySchedule":
        return replace(self, is_planned=flag)

    def activity_at(self, moment: datetime) -> ScheduleActivity | None:
        """Return the activity that covers ``moment``, if any.

        A gap between activities returns ``None`` — callers should treat
        that as "idle / unscheduled".
        """
        for activity in self.activities:
            if activity.contains(moment):
                return activity
        return None

    def upcoming(self, moment: datetime, within: timedelta | None = None) -> list[ScheduleActivity]:
        """Return activities starting after ``moment``, in chronological order.

        ``within`` optionally caps the look-ahead window.
        """
        instant = moment.astimezone(timezone.utc) if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
        horizon = instant + within if within is not None else None
        return [
            a for a in self.activities
            if a.start_at > instant and (horizon is None or a.start_at <= horizon)
        ]

    def most_recent_past(
        self, moment: datetime, *, within: timedelta | None = None,
    ) -> ScheduleActivity | None:
        """Return the most recently ended activity before ``moment``.

        Used by prompt builders when the character is in a gap between
        activities — the model needs to know what just wrapped up to
        generate natural transition lines ("剛從 X 回來，還有點時間…").
        ``within`` caps how far back to look so a morning meeting doesn't
        keep surfacing at night; default is no cap (useful for tests).
        Returns ``None`` when no activity has ended yet today.
        """
        instant = moment.astimezone(timezone.utc) if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
        floor = instant - within if within is not None else None
        past = [
            a for a in self.activities
            if a.end_at <= instant and (floor is None or a.end_at >= floor)
        ]
        if not past:
            return None
        return max(past, key=lambda a: a.end_at)

    def with_activities(self, activities: list[ScheduleActivity]) -> "DailySchedule":
        ordered = tuple(sorted(activities, key=lambda a: a.start_at))
        return replace(self, activities=ordered)
