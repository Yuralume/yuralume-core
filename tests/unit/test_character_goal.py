"""Unit tests for the GoalStatus VO and CharacterGoal entity."""

from datetime import datetime, timezone

import pytest

from kokoro_link.domain.entities.character_goal import (
    ORIGIN_LLM_REVIEW,
    ORIGIN_MANUAL,
    CharacterGoal,
)
from kokoro_link.domain.value_objects.goal_status import (
    CANONICAL_STATUSES,
    GoalStatus,
)


class TestGoalStatus:
    def test_canonical_constants_have_expected_values(self) -> None:
        assert GoalStatus.ACTIVE.value == "active"
        assert GoalStatus.PAUSED.value == "paused"
        assert GoalStatus.DONE.value == "done"
        assert GoalStatus.ABANDONED.value == "abandoned"

    def test_normalises_whitespace_and_case(self) -> None:
        assert GoalStatus.from_string("  ACTIVE  ").value == "active"
        assert GoalStatus.from_string("Paused").value == "paused"

    def test_empty_value_rejected(self) -> None:
        with pytest.raises(ValueError):
            GoalStatus("")
        with pytest.raises(ValueError):
            GoalStatus("   ")

    def test_is_terminal(self) -> None:
        assert GoalStatus.DONE.is_terminal
        assert GoalStatus.ABANDONED.is_terminal
        assert not GoalStatus.ACTIVE.is_terminal
        assert not GoalStatus.PAUSED.is_terminal

    def test_equality(self) -> None:
        assert GoalStatus.from_string("active") == GoalStatus.ACTIVE

    def test_canonical_statuses_enumerates_all(self) -> None:
        assert set(CANONICAL_STATUSES) == {
            GoalStatus.ACTIVE,
            GoalStatus.PAUSED,
            GoalStatus.DONE,
            GoalStatus.ABANDONED,
        }


class TestCharacterGoalCreate:
    def test_defaults_status_to_active_and_priority_to_3(self) -> None:
        goal = CharacterGoal.create(character_id="c1", content="say hello")
        assert goal.status == GoalStatus.ACTIVE
        assert goal.priority == 3
        assert goal.origin == ORIGIN_MANUAL
        assert goal.is_active
        assert goal.last_progressed_at is None

    def test_trims_content(self) -> None:
        goal = CharacterGoal.create(character_id="c1", content="  grow  ")
        assert goal.content == "grow"

    def test_empty_content_rejected(self) -> None:
        with pytest.raises(ValueError):
            CharacterGoal.create(character_id="c1", content="   ")

    def test_priority_clamped_to_range(self) -> None:
        low = CharacterGoal.create(character_id="c1", content="x", priority=-4)
        high = CharacterGoal.create(character_id="c1", content="x", priority=99)
        assert low.priority == 1
        assert high.priority == 5

    def test_tags_stored_as_tuple(self) -> None:
        goal = CharacterGoal.create(
            character_id="c1", content="x", tags=["a", "b"]
        )
        assert goal.tags == ("a", "b")

    def test_assigns_unique_ids(self) -> None:
        a = CharacterGoal.create(character_id="c1", content="x")
        b = CharacterGoal.create(character_id="c1", content="y")
        assert a.id != b.id


class TestCharacterGoalTransitions:
    def _goal(self) -> CharacterGoal:
        return CharacterGoal.create(
            character_id="c1", content="practice guitar",
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )

    def test_with_status_done_stamps_progressed_at(self) -> None:
        goal = self._goal()
        updated = goal.with_status(GoalStatus.DONE, notes="made it")
        assert updated.status == GoalStatus.DONE
        assert updated.review_notes == "made it"
        assert updated.last_progressed_at is not None

    def test_with_status_paused_does_not_stamp_progressed_at(self) -> None:
        goal = self._goal()
        updated = goal.with_status(GoalStatus.PAUSED)
        assert updated.status == GoalStatus.PAUSED
        assert updated.last_progressed_at == goal.last_progressed_at  # both None

    def test_with_status_abandoned_does_not_stamp_progressed_at(self) -> None:
        goal = self._goal()
        updated = goal.with_status(GoalStatus.ABANDONED, notes="no longer relevant")
        assert updated.status == GoalStatus.ABANDONED
        assert updated.last_progressed_at is None
        assert updated.review_notes == "no longer relevant"

    def test_with_status_preserves_notes_when_none_passed(self) -> None:
        goal = self._goal().with_status(GoalStatus.DONE, notes="done")
        again = goal.with_status(GoalStatus.PAUSED)
        assert again.review_notes == "done"

    def test_with_content_trims_and_rejects_empty(self) -> None:
        goal = self._goal()
        updated = goal.with_content("  new content  ")
        assert updated.content == "new content"
        with pytest.raises(ValueError):
            goal.with_content("")

    def test_with_priority_clamps(self) -> None:
        goal = self._goal()
        assert goal.with_priority(10).priority == 5
        assert goal.with_priority(0).priority == 1

    def test_origin_label_for_llm_review(self) -> None:
        goal = CharacterGoal.create(
            character_id="c1", content="x", origin=ORIGIN_LLM_REVIEW
        )
        assert goal.origin == ORIGIN_LLM_REVIEW
