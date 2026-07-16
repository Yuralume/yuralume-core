"""Routes for real character-to-character relationships and encounters."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status

from kokoro_link.api.dependencies import (
    ensure_character_id_owned_by_user,
    ensure_owned_character_id,
    get_container,
    get_current_user_id,
    require_admin,
)
from kokoro_link.application.dto.character_relationship import (
    CharacterEncounterResponse,
    CharacterEncounterTickResponse,
    CharacterRelationshipResponse,
    CreateCharacterRelationshipRequest,
    UpdateCharacterRelationshipRequest,
)
from kokoro_link.application.services.character_relationship_service import (
    CharacterRelationshipNotFoundError,
    CharacterRelationshipUpdate,
    CharacterRelationshipValidationError,
)
from kokoro_link.bootstrap.container import ServiceContainer

router = APIRouter(tags=["character-relationships"])


@router.get(
    "/characters/{character_id}/relationships",
    response_model=list[CharacterRelationshipResponse],
)
async def list_character_relationships(
    character_id: str,
    container: ServiceContainer = Depends(get_container),
    _owned_character_id: str = Depends(ensure_owned_character_id),
) -> list[CharacterRelationshipResponse]:
    try:
        rows = await container.character_relationship_service.list_for_character(
            character_id,
        )
    except CharacterRelationshipNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Character not found",
        ) from exc
    return [CharacterRelationshipResponse.from_domain(row) for row in rows]


@router.post(
    "/characters/{character_id}/relationships",
    response_model=CharacterRelationshipResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_character_relationship(
    character_id: str,
    payload: CreateCharacterRelationshipRequest,
    container: ServiceContainer = Depends(get_container),
    _owned_character_id: str = Depends(ensure_owned_character_id),
    current_user_id: str = Depends(get_current_user_id),
) -> CharacterRelationshipResponse:
    await ensure_character_id_owned_by_user(
        payload.target_character_id,
        current_user_id,
        container,
    )
    try:
        relationship = await container.character_relationship_service.create_or_enable(
            character_id=character_id,
            target_character_id=payload.target_character_id,
            relationship_label=payload.relationship_label,
            how_a_sees_b=payload.how_a_sees_b,
            how_b_sees_a=payload.how_b_sees_a,
        )
    except CharacterRelationshipValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except CharacterRelationshipNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Character not found",
        ) from exc
    if payload.peer_profile_seed is not None:
        social_knowledge = getattr(
            container,
            "character_social_knowledge_service",
            None,
        )
        if social_knowledge is not None:
            await social_knowledge.seed_peer_profile(
                character_id=character_id,
                peer_character_id=payload.target_character_id,
                seed=payload.peer_profile_seed.to_domain(),
            )
    return CharacterRelationshipResponse.from_domain(relationship)


@router.patch(
    "/character-relationships/{relationship_id}",
    response_model=CharacterRelationshipResponse,
)
async def update_character_relationship(
    relationship_id: str,
    payload: UpdateCharacterRelationshipRequest,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> CharacterRelationshipResponse:
    repository = getattr(container, "character_relationship_repository", None)
    if repository is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="character relationship repository not wired",
        )
    existing = await repository.get(relationship_id)
    if existing is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Relationship not found",
        )
    await ensure_character_id_owned_by_user(
        existing.character_a_id,
        current_user_id,
        container,
    )
    await ensure_character_id_owned_by_user(
        existing.character_b_id,
        current_user_id,
        container,
    )
    try:
        relationship = await container.character_relationship_service.update(
            relationship_id,
            CharacterRelationshipUpdate(
                enabled=payload.enabled,
                relationship_label=payload.relationship_label,
                how_a_sees_b=payload.how_a_sees_b,
                how_b_sees_a=payload.how_b_sees_a,
                affection_a_to_b=payload.affection_a_to_b,
                affection_b_to_a=payload.affection_b_to_a,
                trust_a_to_b=payload.trust_a_to_b,
                trust_b_to_a=payload.trust_b_to_a,
            ),
        )
    except CharacterRelationshipNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Relationship not found",
        ) from exc
    return CharacterRelationshipResponse.from_domain(relationship)


@router.get(
    "/characters/{character_id}/encounters",
    response_model=list[CharacterEncounterResponse],
)
async def list_character_encounters(
    character_id: str,
    limit: int = Query(default=30, ge=1, le=200),
    container: ServiceContainer = Depends(get_container),
    _owned_character_id: str = Depends(ensure_owned_character_id),
) -> list[CharacterEncounterResponse]:
    rows = await container.character_encounter_service.list_for_character(
        character_id, limit=limit,
    )
    return [CharacterEncounterResponse.from_domain(row) for row in rows]


@router.post(
    "/admin/character-encounters/tick",
    response_model=CharacterEncounterTickResponse,
)
async def tick_character_encounters(
    container: ServiceContainer = Depends(get_container),
    _admin: object = Depends(require_admin),
) -> CharacterEncounterTickResponse:
    result = await container.character_encounter_service.tick()
    return CharacterEncounterTickResponse.from_domain(result)
