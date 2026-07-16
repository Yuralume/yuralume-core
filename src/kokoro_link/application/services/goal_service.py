"""Goal application service.

Handles manual goal CRUD plus reviewer-driven status changes. The
reviewer trigger lives in ``ChatService`` (every N turns); this service
just applies proposals and records the outcome.
"""

from __future__ import annotations

import logging

from kokoro_link.application.dto.goal import (
    CreateGoalRequest,
    GoalResponse,
    UpdateGoalRequest,
    parse_status,
)
from kokoro_link.contracts.goal_repository import GoalRepositoryPort
from kokoro_link.contracts.goal_reviewer import GoalReviewResult
from kokoro_link.domain.entities.character_goal import (
    ORIGIN_LLM_REVIEW,
    ORIGIN_MANUAL,
    CharacterGoal,
)
from kokoro_link.domain.value_objects.goal_status import GoalStatus

_LOGGER = logging.getLogger(__name__)


class GoalService:
    def __init__(self, repository: GoalRepositoryPort) -> None:
        self._repository = repository

    async def list_goals(self, character_id: str) -> list[GoalResponse]:
        goals = await self._repository.list_for_character(character_id)
        return [GoalResponse.from_domain(g) for g in goals]

    async def list_active_goals(self, character_id: str) -> list[CharacterGoal]:
        return await self._repository.list_for_character(
            character_id, statuses=(GoalStatus.ACTIVE,)
        )

    async def list_all_goals(self, character_id: str) -> list[CharacterGoal]:
        """Return every goal (all statuses) — used by the turn-undo snapshot."""
        return await self._repository.list_for_character(character_id)

    async def get_goal(self, goal_id: str) -> CharacterGoal | None:
        return await self._repository.get(goal_id)

    async def create_goal(
        self,
        character_id: str,
        payload: CreateGoalRequest,
    ) -> GoalResponse:
        goal = CharacterGoal.create(
            character_id=character_id,
            content=payload.content,
            priority=payload.priority,
            tags=payload.tags,
            origin=ORIGIN_MANUAL,
        )
        await self._repository.add(goal)
        return GoalResponse.from_domain(goal)

    async def update_goal(
        self,
        goal_id: str,
        payload: UpdateGoalRequest,
    ) -> GoalResponse | None:
        goal = await self._repository.get(goal_id)
        if goal is None:
            return None

        if payload.content is not None:
            goal = goal.with_content(payload.content)
        if payload.priority is not None:
            goal = goal.with_priority(payload.priority)
        if payload.status is not None:
            try:
                status = parse_status(payload.status)
            except ValueError:
                return None
            goal = goal.with_status(status, notes=payload.notes)
        elif payload.notes is not None:
            from dataclasses import replace
            goal = replace(goal, review_notes=payload.notes)

        await self._repository.save(goal)
        return GoalResponse.from_domain(goal)

    async def delete_goal(self, goal_id: str) -> bool:
        return await self._repository.delete(goal_id)

    async def apply_review_result(
        self,
        *,
        character_id: str,
        result: GoalReviewResult,
    ) -> None:
        """Persist the reviewer's verdicts and new-goal proposals."""
        for change in result.status_changes:
            goal = await self._repository.get(change.goal_id)
            if goal is None or goal.character_id != character_id:
                continue
            if goal.status == change.new_status:
                continue
            updated = goal.with_status(change.new_status, notes=change.notes)
            try:
                await self._repository.save(updated)
            except Exception:
                _LOGGER.exception("Failed to persist goal status change")

        new_goals: list[CharacterGoal] = []
        for proposal in result.new_goals:
            try:
                new_goals.append(
                    CharacterGoal.create(
                        character_id=character_id,
                        content=proposal.content,
                        priority=proposal.priority,
                        tags=proposal.tags,
                        origin=ORIGIN_LLM_REVIEW,
                    )
                )
            except ValueError:
                continue
        if new_goals:
            try:
                await self._repository.add_many(new_goals)
            except Exception:
                _LOGGER.exception("Failed to persist newly proposed goals")
