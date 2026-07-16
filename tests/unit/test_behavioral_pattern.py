"""Unit tests for the BehavioralPattern stack (HUMANIZATION_ROADMAP §3.3)."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from kokoro_link.application.services.behavioral_pattern_service import (
    BehavioralPatternObserverService,
)
from kokoro_link.bootstrap.settings import HumanizationSettings
from kokoro_link.domain.entities.behavioral_pattern import (
    KIND_PHRASE_HABIT,
    KIND_RECURRING_ACTIVITY,
    KIND_TIME_PREFERENCE,
    BehavioralPattern,
)
from kokoro_link.domain.entities.conversation import (
    Message,
    MessageRole,
)
from kokoro_link.domain.entities.schedule import (
    DailySchedule,
    ScheduleActivity,
)
from kokoro_link.infrastructure.repositories.in_memory_behavioral_patterns import (
    InMemoryBehavioralPatternRepository,
)
from kokoro_link.infrastructure.behavior.llm_phrase_habit_extractor import (
    _build_prompt as _build_phrase_habit_prompt,
)


_CHAR = "char-A"
_NOW = datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc)


# ---- entity ----------------------------------------------------------------


def test_entity_rejects_unknown_kind():
    with pytest.raises(ValueError, match="kind"):
        BehavioralPattern.new(
            character_id=_CHAR,
            kind="bogus",
            description="x",
        )


def test_entity_clamps_salience_and_floor_observed_count():
    pattern = BehavioralPattern.new(
        character_id=_CHAR,
        kind=KIND_RECURRING_ACTIVITY,
        description="星期一早晨常運動",
        observed_count=0,
        salience=2.5,
    )
    assert pattern.observed_count == 1
    assert pattern.salience == 1.0


def test_entity_reinforced_bumps_counter():
    pattern = BehavioralPattern.new(
        character_id=_CHAR,
        kind=KIND_RECURRING_ACTIVITY,
        description="星期五晚上常聚餐",
        observed_count=3,
        first_observed_at=_NOW,
        last_observed_at=_NOW,
    )
    later = _NOW + timedelta(days=7)
    bumped = pattern.reinforced(now=later, salience=0.9)
    assert bumped.observed_count == 4
    assert bumped.last_observed_at == later
    assert bumped.salience == 0.9
    # Original immutable.
    assert pattern.observed_count == 3


def test_phrase_habit_extractor_prompt_rejects_style_emotion_feedback() -> None:
    prompt = _build_phrase_habit_prompt(
        character_name="小南",
        recent_lines=[f"角色回覆第 {i}" for i in range(8)],
    )

    assert "只保留內容性" in prompt
    assert "不要把情緒溫度" in prompt
    assert "療癒文風" in prompt
    assert "不是角色特有短句或稱呼" in prompt


# ---- repo ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_repo_upsert_merges_observed_count():
    repo = InMemoryBehavioralPatternRepository()
    p1 = BehavioralPattern.new(
        character_id=_CHAR, kind=KIND_RECURRING_ACTIVITY,
        description="星期一早晨常運動", observed_count=3,
        salience=0.4, last_observed_at=_NOW,
    )
    p2 = BehavioralPattern.new(
        character_id=_CHAR, kind=KIND_RECURRING_ACTIVITY,
        description="星期一早晨常運動", observed_count=2,
        salience=0.7, last_observed_at=_NOW + timedelta(days=7),
    )
    await repo.upsert(p1)
    merged = await repo.upsert(p2)
    assert merged.observed_count == 5
    assert merged.salience == 0.7
    assert merged.last_observed_at == _NOW + timedelta(days=7)


@pytest.mark.asyncio
async def test_repo_list_filters_by_kind_and_orders_by_weight():
    repo = InMemoryBehavioralPatternRepository()
    await repo.upsert(BehavioralPattern.new(
        character_id=_CHAR, kind=KIND_PHRASE_HABIT,
        description="結尾常加『欸』", observed_count=2, salience=0.6,
    ))
    await repo.upsert(BehavioralPattern.new(
        character_id=_CHAR, kind=KIND_RECURRING_ACTIVITY,
        description="星期一早晨常運動", observed_count=6, salience=0.8,
    ))
    await repo.upsert(BehavioralPattern.new(
        character_id=_CHAR, kind=KIND_RECURRING_ACTIVITY,
        description="星期五晚上常出門", observed_count=3, salience=0.5,
    ))

    only_recurring = await repo.list_for_character(
        _CHAR, kinds=(KIND_RECURRING_ACTIVITY,),
    )
    descriptions = [p.description for p in only_recurring]
    assert descriptions[0] == "星期一早晨常運動"
    assert "星期五晚上常出門" in descriptions
    # Phrase habit excluded by filter.
    assert all(p.kind == KIND_RECURRING_ACTIVITY for p in only_recurring)


# ---- service ---------------------------------------------------------------


def _activity(local_dt: datetime, *, category: str) -> ScheduleActivity:
    return ScheduleActivity.create(
        start_at=local_dt,
        end_at=local_dt + timedelta(hours=1),
        description=f"做 {category}",
        category=category,
    )


def _build_schedules_with_routine() -> list[DailySchedule]:
    """Four consecutive Mondays of "early-morning study" + assorted noise.

    Should clear the ``_RECURRING_THRESHOLD`` (3) for both
    ``(星期一, study)`` and ``(清晨, *)``.
    """
    schedules: list[DailySchedule] = []
    monday = date(2026, 4, 6)  # known Monday
    for week in range(4):
        target = monday + timedelta(weeks=week)
        local_dt = datetime.combine(
            target, time(6, 30), tzinfo=timezone.utc,
        )
        schedules.append(DailySchedule.create(
            character_id=_CHAR,
            date_=target,
            activities=[
                _activity(local_dt, category="study"),
                _activity(local_dt + timedelta(hours=4), category="work"),
            ],
            is_planned=True,
        ))
    return schedules


@pytest.mark.asyncio
async def test_observe_writes_recurring_activity_pattern_above_threshold():
    schedule_repo = AsyncMock()
    schedule_repo.list_for_character = AsyncMock(
        return_value=_build_schedules_with_routine(),
    )
    behavioural_repo = InMemoryBehavioralPatternRepository()

    svc = BehavioralPatternObserverService(
        repository=behavioural_repo,
        schedule_repository=schedule_repo,
        settings=HumanizationSettings(),
    )

    summary = await svc.observe_for_character(_CHAR, now=_NOW)

    assert summary[KIND_RECURRING_ACTIVITY] >= 1
    rows = await behavioural_repo.list_for_character(_CHAR)
    descriptions = [r.description for r in rows]
    assert any("星期一" in d and "study" in d for d in descriptions)


@pytest.mark.asyncio
async def test_observe_schedule_patterns_uses_owner_timezone():
    schedule_repo = AsyncMock()
    local_tz = timezone(timedelta(hours=8))
    schedules = []
    for day in (date(2026, 6, 15), date(2026, 6, 22), date(2026, 6, 29)):
        local_start = datetime.combine(day, time(0, 30), tzinfo=local_tz)
        schedules.append(DailySchedule.create(
            character_id=_CHAR,
            date_=day,
            activities=[
                _activity(local_start.astimezone(timezone.utc), category="study"),
            ],
            is_planned=True,
        ))
    schedule_repo.list_for_character = AsyncMock(return_value=schedules)
    behavioural_repo = InMemoryBehavioralPatternRepository()
    svc = BehavioralPatternObserverService(
        repository=behavioural_repo,
        schedule_repository=schedule_repo,
        settings=HumanizationSettings(),
        local_tz=timezone.utc,
    )

    await svc.observe_for_character(
        _CHAR,
        now=datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc),
        local_tz=local_tz,
    )

    rows = await behavioural_repo.list_for_character(_CHAR)
    descriptions = [r.description for r in rows]
    assert any("星期一" in d and "study" in d for d in descriptions)
    assert not any("星期日" in d and "study" in d for d in descriptions)


@pytest.mark.asyncio
async def test_observe_schedule_patterns_ignores_future_activities():
    """Future plans are not observed behaviour yet.

    Regression coverage for dream ticks that run before a planned day:
    counting future activities would create ``first_observed_at > now`` and
    violate the BehavioralPattern invariant.
    """
    schedule_repo = AsyncMock()
    future_monday = date(2026, 6, 15)
    schedule_repo.list_for_character = AsyncMock(return_value=[
        DailySchedule.create(
            character_id=_CHAR,
            date_=future_monday + timedelta(weeks=week),
            activities=[
                _activity(
                    datetime.combine(
                        future_monday + timedelta(weeks=week),
                        time(6, 30),
                        tzinfo=timezone.utc,
                    ),
                    category="study",
                ),
            ],
            is_planned=True,
        )
        for week in range(3)
    ])
    behavioural_repo = InMemoryBehavioralPatternRepository()
    svc = BehavioralPatternObserverService(
        repository=behavioural_repo,
        schedule_repository=schedule_repo,
        settings=HumanizationSettings(),
    )

    summary = await svc.observe_for_character(
        _CHAR,
        now=datetime(2026, 6, 12, 1, 33, tzinfo=timezone.utc),
    )

    assert summary[KIND_RECURRING_ACTIVITY] == 0
    assert summary[KIND_TIME_PREFERENCE] == 0
    assert await behavioural_repo.list_for_character(_CHAR) == []


@pytest.mark.asyncio
async def test_observe_skips_below_threshold_categories():
    """Only one occurrence of ``cooking`` → must not anchor a pattern."""
    schedule_repo = AsyncMock()
    schedule_repo.list_for_character = AsyncMock(return_value=[
        DailySchedule.create(
            character_id=_CHAR,
            date_=date(2026, 5, 6),
            activities=[
                _activity(
                    datetime(2026, 5, 6, 18, tzinfo=timezone.utc),
                    category="cooking",
                ),
            ],
            is_planned=True,
        ),
    ])
    behavioural_repo = InMemoryBehavioralPatternRepository()

    svc = BehavioralPatternObserverService(
        repository=behavioural_repo,
        schedule_repository=schedule_repo,
        settings=HumanizationSettings(),
    )

    await svc.observe_for_character(_CHAR, now=_NOW)
    rows = await behavioural_repo.list_for_character(_CHAR)
    assert rows == []


@pytest.mark.asyncio
async def test_feature_flag_off_short_circuits():
    schedule_repo = AsyncMock()
    behavioural_repo = InMemoryBehavioralPatternRepository()
    svc = BehavioralPatternObserverService(
        repository=behavioural_repo,
        schedule_repository=schedule_repo,
        settings=HumanizationSettings(behavioral_pattern_enabled=False),
    )

    summary = await svc.observe_for_character(_CHAR, now=_NOW)

    assert summary == {}
    schedule_repo.list_for_character.assert_not_called()


@pytest.mark.asyncio
async def test_phrase_habit_extractor_invoked_when_wired():
    schedule_repo = AsyncMock()
    schedule_repo.list_for_character = AsyncMock(return_value=[])

    conv_repo = MagicMock()

    def _msg(content: str) -> Message:
        return Message(
            role=MessageRole.ASSISTANT,
            content=content,
            created_at=_NOW,
        )

    conv_repo.recent_messages_for_character = AsyncMock(return_value=[
        _msg(f"回應第 {i} 句") for i in range(20)
    ])

    extractor = MagicMock()
    extractor.extract = AsyncMock(return_value=["結尾常加『欸』", "開場常用『嗯～』"])

    behavioural_repo = InMemoryBehavioralPatternRepository()
    svc = BehavioralPatternObserverService(
        repository=behavioural_repo,
        schedule_repository=schedule_repo,
        conversation_repository=conv_repo,
        phrase_habit_extractor=extractor,
        settings=HumanizationSettings(),
    )

    summary = await svc.observe_for_character(
        _CHAR, character_name="小南", now=_NOW,
    )

    assert summary[KIND_PHRASE_HABIT] == 2
    rows = await behavioural_repo.list_for_character(_CHAR)
    phrase_rows = [r for r in rows if r.kind == KIND_PHRASE_HABIT]
    descriptions = {r.description for r in phrase_rows}
    assert descriptions == {"結尾常加『欸』", "開場常用『嗯～』"}
    extractor.extract.assert_awaited_once()
    call_kwargs = extractor.extract.await_args.kwargs
    assert call_kwargs["character_name"] == "小南"
    # Only assistant lines were passed through.
    assert all("回應第" in line for line in call_kwargs["recent_lines"])
