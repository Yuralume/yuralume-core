"""Tests for busy_score defaults and prompt tone hints."""

from __future__ import annotations

from datetime import date, datetime, timezone

from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.conversation import Conversation
from kokoro_link.domain.entities.schedule import (
    DEFAULT_UNKNOWN_BUSY_SCORE,
    ScheduleActivity,
    default_busy_score,
)
from kokoro_link.application.services.turn_snapshot_codec import schedule_from_dict
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.prompt.default import DefaultPromptContextBuilder


def _activity(category: str, busy: float | None = None) -> ScheduleActivity:
    return ScheduleActivity.create(
        start_at=datetime(2026, 4, 18, 9, 0, tzinfo=timezone.utc),
        end_at=datetime(2026, 4, 18, 11, 0, tzinfo=timezone.utc),
        description="測試活動",
        category=category,
        busy_score=busy,
    )


class TestDefaultBusyScore:
    def test_sleep_returns_high_reply_cost(self) -> None:
        assert default_busy_score("sleep") >= 0.9
        assert default_busy_score("午睡") >= 0.9

    def test_work_is_reachable_mid_cost(self) -> None:
        assert 0.4 <= default_busy_score("work") < 0.7
        assert 0.4 <= default_busy_score("工作") < 0.7

    def test_meeting_returns_high_but_not_absolute_cost(self) -> None:
        assert 0.7 <= default_busy_score("meeting") < 0.9
        assert 0.7 <= default_busy_score("會議") < 0.9

    def test_no_phone_categories_return_highest(self) -> None:
        assert default_busy_score("drive") >= 0.9
        assert default_busy_score("考試") >= 0.9

    def test_unknown_returns_reachable_default(self) -> None:
        assert default_busy_score("觀星") == DEFAULT_UNKNOWN_BUSY_SCORE
        assert default_busy_score("空氣蛙泳") == DEFAULT_UNKNOWN_BUSY_SCORE

    def test_empty_returns_reachable_default(self) -> None:
        assert default_busy_score("") == DEFAULT_UNKNOWN_BUSY_SCORE

    def test_mixed_case_and_surrounding_text(self) -> None:
        # e.g., "deep work" should still match "work" stem
        assert 0.4 <= default_busy_score("Deep Work") < 0.7


class TestScheduleActivityBusy:
    def test_explicit_busy_score_preserved(self) -> None:
        activity = _activity("work", busy=0.2)
        assert activity.busy_score == 0.2

    def test_omitted_busy_score_falls_back_to_category_default(self) -> None:
        activity = _activity("work")
        assert 0.4 <= activity.busy_score < 0.7

    def test_busy_score_clamped(self) -> None:
        activity = _activity("work", busy=5.0)
        assert activity.busy_score == 1.0
        activity = _activity("work", busy=-0.3)
        assert activity.busy_score == 0.0

    def test_direct_constructor_missing_busy_score_uses_unknown_default(self) -> None:
        activity = ScheduleActivity(
            id="activity-1",
            start_at=datetime(2026, 4, 18, 9, 0, tzinfo=timezone.utc),
            end_at=datetime(2026, 4, 18, 10, 0, tzinfo=timezone.utc),
            description="臨時活動",
            category="unknown",
            busy_score=None,  # type: ignore[arg-type]
        )
        assert activity.busy_score == DEFAULT_UNKNOWN_BUSY_SCORE

    def test_snapshot_restore_missing_busy_score_uses_unknown_default(self) -> None:
        restored = schedule_from_dict(
            {
                "id": "schedule-1",
                "character_id": "character-1",
                "date": date(2026, 4, 18).isoformat(),
                "generated_at": datetime(2026, 4, 18, 0, 0, tzinfo=timezone.utc).isoformat(),
                "activities": [
                    {
                        "id": "activity-1",
                        "start_at": datetime(2026, 4, 18, 9, 0, tzinfo=timezone.utc).isoformat(),
                        "end_at": datetime(2026, 4, 18, 10, 0, tzinfo=timezone.utc).isoformat(),
                        "description": "舊 snapshot",
                        "category": "unknown",
                    },
                ],
            }
        )
        assert restored.activities[0].busy_score == DEFAULT_UNKNOWN_BUSY_SCORE


def _character() -> Character:
    return Character.create(
        name="Aki",
        summary="",
        personality=[],
        interests=[],
        speaking_style="",
        boundaries=[],
        state=CharacterState(emotion="neutral", affection=50, fatigue=0, trust=50, energy=100),
    )


class TestPromptBusyHint:
    def test_high_busy_suggests_brief_reply(self) -> None:
        builder = DefaultPromptContextBuilder()
        character = _character()
        activity = ScheduleActivity.create(
            start_at=datetime(2026, 4, 18, 9, 0, tzinfo=timezone.utc),
            end_at=datetime(2026, 4, 18, 11, 0, tzinfo=timezone.utc),
            description="趕稿",
            category="deadline",
            busy_score=0.95,
        )
        prompt = builder.build(
            character=character,
            conversation=Conversation.start(character_id=character.id),
            recent_messages=[],
            memories=[],
            pending_state=character.state,
            latest_user_message="在嗎",
            current_activity=activity,
        )
        assert "忙碌程度：非常高" in prompt or "簡短" in prompt

    def test_idle_suggests_relaxed_reply(self) -> None:
        builder = DefaultPromptContextBuilder()
        character = _character()
        activity = ScheduleActivity.create(
            start_at=datetime(2026, 4, 18, 22, 0, tzinfo=timezone.utc),
            end_at=datetime(2026, 4, 18, 23, 0, tzinfo=timezone.utc),
            description="聽音樂",
            category="leisure",
            busy_score=0.15,
        )
        prompt = builder.build(
            character=character,
            conversation=Conversation.start(character_id=character.id),
            recent_messages=[],
            memories=[],
            pending_state=character.state,
            latest_user_message="嗨",
            current_activity=activity,
        )
        assert "忙碌程度" in prompt
        # Low-busy hint should mention relaxed/idle language
        assert "放鬆" in prompt or "餘裕" in prompt

    def test_idle_with_no_activity_still_emits_hint(self) -> None:
        builder = DefaultPromptContextBuilder()
        character = _character()
        activity = ScheduleActivity.create(
            start_at=datetime(2026, 4, 18, 14, 0, tzinfo=timezone.utc),
            end_at=datetime(2026, 4, 18, 15, 0, tzinfo=timezone.utc),
            description="工作",
            category="work",
        )
        prompt = builder.build(
            character=character,
            conversation=Conversation.start(character_id=character.id),
            recent_messages=[],
            memories=[],
            pending_state=character.state,
            latest_user_message="hi",
            current_activity=None,
            upcoming_activities=[activity],
        )
        assert "忙碌程度：低" in prompt
