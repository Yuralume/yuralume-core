from fastapi import APIRouter, Depends, HTTPException, Response, status

from kokoro_link.api.dependencies import (
    ensure_character_id_owned_by_user,
    ensure_owned_character_id,
    get_container,
    get_current_user_id,
)
from kokoro_link.application.dto.goal import (
    CreateGoalRequest,
    GoalResponse,
    UpdateGoalRequest,
)
from kokoro_link.bootstrap.container import ServiceContainer

router = APIRouter(tags=["goals"])


@router.get("/characters/{character_id}/goals", response_model=list[GoalResponse])
async def list_goals(
    character_id: str,
    container: ServiceContainer = Depends(get_container),
    _owned_character_id: str = Depends(ensure_owned_character_id),
) -> list[GoalResponse]:
    return await container.goal_service.list_goals(character_id)


@router.post(
    "/characters/{character_id}/goals",
    response_model=GoalResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_goal(
    character_id: str,
    payload: CreateGoalRequest,
    container: ServiceContainer = Depends(get_container),
    _owned_character_id: str = Depends(ensure_owned_character_id),
) -> GoalResponse:
    return await container.goal_service.create_goal(character_id, payload)


@router.patch("/goals/{goal_id}", response_model=GoalResponse)
async def update_goal(
    goal_id: str,
    payload: UpdateGoalRequest,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> GoalResponse:
    existing = await container.goal_service.get_goal(goal_id)
    if existing is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Goal not found")
    await ensure_character_id_owned_by_user(
        existing.character_id,
        current_user_id,
        container,
    )
    goal = await container.goal_service.update_goal(goal_id, payload)
    if goal is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Goal not found")
    return goal


@router.delete("/goals/{goal_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_goal(
    goal_id: str,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> Response:
    existing = await container.goal_service.get_goal(goal_id)
    if existing is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Goal not found")
    await ensure_character_id_owned_by_user(
        existing.character_id,
        current_user_id,
        container,
    )
    removed = await container.goal_service.delete_goal(goal_id)
    if not removed:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Goal not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
