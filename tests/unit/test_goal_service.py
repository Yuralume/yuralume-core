"""GoalService unit tests — manual CRUD and reviewer application."""

import pytest

from kokoro_link.application.dto.goal import (
    CreateGoalRequest,
    UpdateGoalRequest,
)
from kokoro_link.application.services.goal_service import GoalService
from kokoro_link.contracts.goal_reviewer import (
    GoalReviewResult,
    GoalStatusChange,
    NewGoalProposal,
)
from kokoro_link.domain.entities.character_goal import (
    ORIGIN_LLM_REVIEW,
    ORIGIN_MANUAL,
    CharacterGoal,
)
from kokoro_link.domain.value_objects.goal_status import GoalStatus
from kokoro_link.infrastructure.repositories.in_memory_goals import (
    InMemoryGoalRepository,
)


def _build_service() -> tuple[GoalService, InMemoryGoalRepository]:
    repo = InMemoryGoalRepository()
    return GoalService(repo), repo


class TestGoalServiceCrud:
    @pytest.mark.asyncio
    async def test_create_returns_goal_marked_manual(self) -> None:
        service, _ = _build_service()
        created = await service.create_goal(
            "c1", CreateGoalRequest(content="practice", priority=4, tags=["music"])
        )
        assert created.content == "practice"
        assert created.priority == 4
        assert created.status == "active"
        assert created.origin == ORIGIN_MANUAL
        assert created.tags == ["music"]

    @pytest.mark.asyncio
    async def test_list_goals_sorts_by_priority_desc(self) -> None:
        service, _ = _build_service()
        await service.create_goal("c1", CreateGoalRequest(content="low", priority=1))
        await service.create_goal("c1", CreateGoalRequest(content="high", priority=5))
        listed = await service.list_goals("c1")
        assert [g.content for g in listed] == ["high", "low"]

    @pytest.mark.asyncio
    async def test_list_active_filters_terminal(self) -> None:
        service, repo = _build_service()
        active = await service.create_goal("c1", CreateGoalRequest(content="a"))
        done_goal = await service.create_goal("c1", CreateGoalRequest(content="b"))
        # Transition one to done
        await service.update_goal(done_goal.id, UpdateGoalRequest(status="done"))
        active_goals = await service.list_active_goals("c1")
        assert len(active_goals) == 1
        assert active_goals[0].id == active.id

    @pytest.mark.asyncio
    async def test_update_invalid_status_returns_none(self) -> None:
        service, _ = _build_service()
        goal = await service.create_goal("c1", CreateGoalRequest(content="x"))
        result = await service.update_goal(
            goal.id, UpdateGoalRequest(status="   ")
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_update_missing_goal_returns_none(self) -> None:
        service, _ = _build_service()
        result = await service.update_goal("no-such-id", UpdateGoalRequest(content="x"))
        assert result is None

    @pytest.mark.asyncio
    async def test_update_notes_only_preserves_status(self) -> None:
        service, _ = _build_service()
        goal = await service.create_goal("c1", CreateGoalRequest(content="x"))
        updated = await service.update_goal(
            goal.id, UpdateGoalRequest(notes="keep going")
        )
        assert updated is not None
        assert updated.status == "active"
        assert updated.review_notes == "keep going"

    @pytest.mark.asyncio
    async def test_delete_goal_removes_it(self) -> None:
        service, _ = _build_service()
        goal = await service.create_goal("c1", CreateGoalRequest(content="x"))
        assert await service.delete_goal(goal.id) is True
        assert await service.delete_goal(goal.id) is False


class TestGoalServiceApplyReview:
    @pytest.mark.asyncio
    async def test_applies_status_change_for_known_goal(self) -> None:
        service, repo = _build_service()
        goal = await service.create_goal("c1", CreateGoalRequest(content="x"))
        await service.apply_review_result(
            character_id="c1",
            result=GoalReviewResult(
                status_changes=[
                    GoalStatusChange(
                        goal_id=goal.id,
                        new_status=GoalStatus.DONE,
                        notes="finished",
                    )
                ],
            ),
        )
        stored = await repo.get(goal.id)
        assert stored is not None
        assert stored.status == GoalStatus.DONE
        assert stored.review_notes == "finished"

    @pytest.mark.asyncio
    async def test_ignores_change_for_foreign_character(self) -> None:
        service, repo = _build_service()
        goal = await service.create_goal("c1", CreateGoalRequest(content="x"))
        await service.apply_review_result(
            character_id="c2",  # mismatched
            result=GoalReviewResult(
                status_changes=[
                    GoalStatusChange(goal_id=goal.id, new_status=GoalStatus.DONE),
                ],
            ),
        )
        stored = await repo.get(goal.id)
        assert stored is not None
        assert stored.status == GoalStatus.ACTIVE

    @pytest.mark.asyncio
    async def test_skips_noop_status_change(self) -> None:
        service, repo = _build_service()
        goal = await service.create_goal("c1", CreateGoalRequest(content="x"))
        original_progressed = goal.last_progressed_at
        await service.apply_review_result(
            character_id="c1",
            result=GoalReviewResult(
                status_changes=[
                    GoalStatusChange(
                        goal_id=goal.id,
                        new_status=GoalStatus.ACTIVE,
                    )
                ],
            ),
        )
        stored = await repo.get(goal.id)
        assert stored is not None
        assert stored.last_progressed_at == original_progressed

    @pytest.mark.asyncio
    async def test_adds_new_goals_with_llm_origin(self) -> None:
        service, repo = _build_service()
        await service.apply_review_result(
            character_id="c1",
            result=GoalReviewResult(
                new_goals=[
                    NewGoalProposal(content="rest more", priority=2),
                    NewGoalProposal(content="", priority=3),  # empty, dropped
                ],
            ),
        )
        stored = await repo.list_for_character("c1")
        assert len(stored) == 1
        assert stored[0].content == "rest more"
        assert stored[0].origin == ORIGIN_LLM_REVIEW
