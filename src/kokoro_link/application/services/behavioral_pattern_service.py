"""Behavioural-pattern observer (HUMANIZATION_ROADMAP §3.3).

Runs at the tail of the dream pass, alongside the relationship-milestone
observer. Two parallel detections:

- ``recurring_activity`` — pure statistics on the character's recent
  ``DailySchedule`` history. We bucket by ``(weekday, category)`` and
  ``(time_bucket, category)`` independently. Any bucket seen ≥
  ``_RECURRING_THRESHOLD`` times is upserted as a pattern.
- ``phrase_habit`` — LLM extractor over the character's recent
  assistant lines (HUMANIZATION_ROADMAP §3.3 verbal habit clause).
  Off when the provider is fake or the feature key flag is off.

LLM-first 紅線: the entity stores facts. Downstream prompt injection
adds these as a fact-layer block; nothing reads ``observed_count`` to
branch behaviour, only to decide top-N ordering.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timezone, tzinfo
from typing import Final

from kokoro_link.bootstrap.settings import HumanizationSettings
from kokoro_link.contracts.behavioral_pattern import (
    BehavioralPatternRepositoryPort,
)
from kokoro_link.contracts.phrase_habit import PhraseHabitExtractorPort
from kokoro_link.contracts.repositories import ConversationRepositoryPort
from kokoro_link.contracts.schedule_repository import ScheduleRepositoryPort
from kokoro_link.domain.entities.behavioral_pattern import (
    KIND_PHRASE_HABIT,
    KIND_RECURRING_ACTIVITY,
    KIND_TIME_PREFERENCE,
    BehavioralPattern,
)
from kokoro_link.domain.entities.conversation import MessageRole
from kokoro_link.domain.value_objects.timezone import to_timezone

_LOGGER = logging.getLogger(__name__)


_RECURRING_THRESHOLD: Final = 3
"""Minimum number of weekday/time-bucket × category co-occurrences before
a recurrence is anchored as a pattern. Below this it is just noise."""

_TIME_PREFERENCE_THRESHOLD: Final = 8
"""Aggregate count of activities in the same time bucket before we call
the character "a morning person / a night owl"."""

_RECENT_SCHEDULES_LIMIT: Final = 28
"""How far back to look. Four weeks is enough for weekday recurrences
without dragging stale rhythms forward."""

_RECENT_MESSAGES_PER_CONVERSATION: Final = 40

_WEEKDAY_LABELS: Final = (
    "星期一", "星期二", "星期三", "星期四",
    "星期五", "星期六", "星期日",
)

# Buckets are intentionally coarse — finer would inflate the unique key
# space and dilute observed_count below threshold.
_TIME_BUCKETS: Final = (
    (0, 5, "清晨前"),
    (5, 9, "清晨"),
    (9, 12, "上午"),
    (12, 14, "中午"),
    (14, 18, "下午"),
    (18, 21, "傍晚"),
    (21, 24, "夜晚"),
)


def _bucket_for_hour(hour: int) -> str:
    for lo, hi, label in _TIME_BUCKETS:
        if lo <= hour < hi:
            return label
    return "夜晚"


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class BehavioralPatternObserverService:
    def __init__(
        self,
        *,
        repository: BehavioralPatternRepositoryPort,
        schedule_repository: ScheduleRepositoryPort,
        conversation_repository: ConversationRepositoryPort | None = None,
        phrase_habit_extractor: PhraseHabitExtractorPort | None = None,
        settings: HumanizationSettings,
        local_tz: tzinfo = timezone.utc,
    ) -> None:
        self._repository = repository
        self._schedule_repository = schedule_repository
        self._conversation_repository = conversation_repository
        self._phrase_habit_extractor = phrase_habit_extractor
        self._settings = settings
        self._local_tz = local_tz

    @property
    def enabled(self) -> bool:
        return self._settings.behavioral_pattern_enabled

    async def observe_for_character(
        self,
        character_id: str,
        *,
        character_name: str = "",
        now: datetime | None = None,
        local_tz: tzinfo | None = None,
    ) -> dict[str, int]:
        """Run one observation pass for one character.

        Returns a small dict with counts per kind for callers that want
        to log / surface activity. Always best-effort — partial failures
        are logged and absorbed so the dream tick stays robust.
        """
        if not self.enabled:
            return {}
        ref = _as_utc(now or datetime.now(timezone.utc))
        summary: dict[str, int] = {}

        effective_tz = local_tz or self._local_tz
        recurring, time_pref = await self._observe_schedule_patterns(
            character_id, ref, local_tz=effective_tz,
        )
        summary[KIND_RECURRING_ACTIVITY] = recurring
        summary[KIND_TIME_PREFERENCE] = time_pref

        if self._phrase_habit_extractor is not None:
            phrase = await self._observe_phrase_habits(
                character_id, character_name=character_name, now=ref,
            )
            summary[KIND_PHRASE_HABIT] = phrase

        return summary

    # ---- schedule statistics ----------------------------------------------

    async def _observe_schedule_patterns(
        self, character_id: str, now: datetime, *, local_tz: tzinfo,
    ) -> tuple[int, int]:
        ref = _as_utc(now)
        try:
            schedules = await self._schedule_repository.list_for_character(
                character_id, limit=_RECENT_SCHEDULES_LIMIT,
            )
        except Exception:
            _LOGGER.exception(
                "behavioral_pattern: schedule history fetch failed character=%s",
                character_id,
            )
            return (0, 0)

        weekday_counter: Counter[tuple[str, str]] = Counter()
        bucket_counter: Counter[str] = Counter()
        first_seen: dict[tuple[str, str], datetime] = {}
        first_bucket_seen: dict[str, datetime] = {}

        for schedule in schedules:
            for activity in schedule.activities:
                activity_start = _as_utc(activity.start_at)
                if activity_start > ref:
                    continue
                local_start = to_timezone(activity_start, local_tz)
                weekday_label = _WEEKDAY_LABELS[local_start.weekday()]
                bucket_label = _bucket_for_hour(local_start.hour)
                category = (activity.category or "").strip()
                if not category:
                    continue
                wk_key = (weekday_label, category)
                weekday_counter[wk_key] += 1
                first_seen.setdefault(wk_key, activity_start)
                if activity_start < first_seen[wk_key]:
                    first_seen[wk_key] = activity_start

                bucket_counter[bucket_label] += 1
                first_bucket_seen.setdefault(bucket_label, activity_start)
                if activity_start < first_bucket_seen[bucket_label]:
                    first_bucket_seen[bucket_label] = activity_start

        recurring_written = 0
        for (weekday_label, category), count in weekday_counter.items():
            if count < _RECURRING_THRESHOLD:
                continue
            description = f"{weekday_label}常做「{category}」"
            pattern = BehavioralPattern.new(
                character_id=character_id,
                kind=KIND_RECURRING_ACTIVITY,
                description=description,
                observed_count=count,
                salience=min(1.0, count / 8.0),
                first_observed_at=first_seen.get((weekday_label, category), ref),
                last_observed_at=ref,
            )
            try:
                await self._repository.upsert(pattern)
                recurring_written += 1
            except Exception:
                _LOGGER.exception(
                    "behavioral_pattern: upsert recurring failed (%s, %s)",
                    weekday_label, category,
                )

        time_pref_written = 0
        for bucket_label, count in bucket_counter.items():
            if count < _TIME_PREFERENCE_THRESHOLD:
                continue
            description = f"{bucket_label}是這位角色最活躍的時段之一"
            pattern = BehavioralPattern.new(
                character_id=character_id,
                kind=KIND_TIME_PREFERENCE,
                description=description,
                observed_count=count,
                salience=min(1.0, count / 16.0),
                first_observed_at=first_bucket_seen.get(bucket_label, ref),
                last_observed_at=ref,
            )
            try:
                await self._repository.upsert(pattern)
                time_pref_written += 1
            except Exception:
                _LOGGER.exception(
                    "behavioral_pattern: upsert time_preference failed (%s)",
                    bucket_label,
                )

        return (recurring_written, time_pref_written)

    # ---- phrase habits ----------------------------------------------------

    async def _observe_phrase_habits(
        self,
        character_id: str,
        *,
        character_name: str,
        now: datetime,
    ) -> int:
        if (
            self._phrase_habit_extractor is None
            or self._conversation_repository is None
        ):
            return 0
        try:
            recent_lines = await self._load_recent_assistant_lines(character_id)
        except Exception:
            _LOGGER.exception(
                "behavioral_pattern: conversation history fetch failed character=%s",
                character_id,
            )
            return 0
        if not recent_lines:
            return 0
        try:
            habits = await self._phrase_habit_extractor.extract(
                character_name=character_name or "（未命名角色）",
                recent_lines=recent_lines,
            )
        except Exception:
            _LOGGER.exception(
                "behavioral_pattern: phrase habit extractor crashed character=%s",
                character_id,
            )
            return 0

        written = 0
        for habit in habits:
            pattern = BehavioralPattern.new(
                character_id=character_id,
                kind=KIND_PHRASE_HABIT,
                description=habit,
                observed_count=1,
                salience=0.6,
                last_observed_at=now,
            )
            try:
                await self._repository.upsert(pattern)
                written += 1
            except Exception:
                _LOGGER.exception(
                    "behavioral_pattern: upsert phrase_habit failed (%s)",
                    habit,
                )
        return written

    async def _load_recent_assistant_lines(
        self, character_id: str,
    ) -> list[str]:
        """Pull a cross-source assistant-line window for the LLM extractor.

        Uses ``recent_messages_for_character`` so the character is read
        as one person across web / telegram / line — same merge rule the
        chat prompt builder honours (see [[feedback_cross_channel_persona]]).
        ``exclude_tool_only=True`` drops bare ``/pic`` artifacts so the
        habit extractor sees prose.
        """
        repo = self._conversation_repository
        if repo is None:
            return []
        try:
            messages = await repo.recent_messages_for_character(
                character_id,
                limit=_RECENT_MESSAGES_PER_CONVERSATION,
                exclude_tool_only=True,
            )
        except Exception:
            _LOGGER.exception(
                "behavioral_pattern: recent_messages_for_character failed",
            )
            return []
        return [
            (m.content or "").strip()
            for m in messages
            if m.role == MessageRole.ASSISTANT and (m.content or "").strip()
        ]
