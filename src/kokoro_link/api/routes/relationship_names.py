"""Player edit of the per-character relationship address names.

``PATCH /characters/{character_id}/relationship-names`` lets the player
change how one character addresses them (``user_address_name``) and how
they address that character (``character_address_name``) after creation.
The heavy lifting (seed save + rename-log + persona reconcile) lives in
``RelationshipNamesService``; this route only owns auth, parsing, and the
response shape.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from kokoro_link.api.dependencies import (
    ensure_character_id_owned_by_user,
    get_container,
    get_current_user_id,
)
from kokoro_link.bootstrap.container import ServiceContainer
from kokoro_link.domain.entities.character_operator_relationship_seed import (
    CharacterOperatorRelationshipSeed,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["relationship-names"])


class RelationshipNamesPatch(BaseModel):
    """Partial edit. Omit a field to leave it unchanged; send an empty
    string to clear it (reverts to the lower-precedence source)."""

    user_address_name: str | None = Field(
        default=None,
        max_length=80,
        description="How the character should address the player.",
    )
    character_address_name: str | None = Field(
        default=None,
        max_length=80,
        description="How the player addresses the character.",
    )


class RelationshipNamesResponse(BaseModel):
    character_id: str
    operator_id: str
    user_address_name: str
    character_address_name: str

    @classmethod
    def from_seed(
        cls, seed: CharacterOperatorRelationshipSeed,
    ) -> "RelationshipNamesResponse":
        return cls(
            character_id=seed.character_id,
            operator_id=seed.operator_id,
            user_address_name=seed.user_address_name,
            character_address_name=seed.character_address_name,
        )


@router.get(
    "/characters/{character_id}/relationship-names",
    response_model=RelationshipNamesResponse,
)
async def get_relationship_names(
    character_id: str,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> RelationshipNamesResponse:
    """Return the current per-(character, operator) address names so the
    editor can pre-fill. Empty strings when nothing has been set."""
    await ensure_character_id_owned_by_user(
        character_id,
        current_user_id,
        container,
    )
    service = getattr(container, "relationship_names_service", None)
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="relationship names service not wired",
        )
    user_name, character_name = await service.get_names(
        character_id=character_id,
        operator_id=current_user_id,
    )
    return RelationshipNamesResponse(
        character_id=character_id,
        operator_id=current_user_id,
        user_address_name=user_name,
        character_address_name=character_name,
    )


@router.patch(
    "/characters/{character_id}/relationship-names",
    response_model=RelationshipNamesResponse,
)
async def update_relationship_names(
    character_id: str,
    payload: RelationshipNamesPatch,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> RelationshipNamesResponse:
    """Update the per-(character, operator) address names.

    Writes a rename-log event for each changed direction and reconciles
    the learned persona name so the prompt doesn't double-render. Scoped
    to the caller's own pair — ``character_id`` is ownership-checked, and
    the seed is keyed to the caller's id, so a Bob can't rename Alice's
    relationship.
    """
    if (
        "user_address_name" not in payload.model_fields_set
        and "character_address_name" not in payload.model_fields_set
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="provide at least one of user_address_name / "
            "character_address_name",
        )
    await ensure_character_id_owned_by_user(
        character_id,
        current_user_id,
        container,
    )
    service = getattr(container, "relationship_names_service", None)
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="relationship names service not wired",
        )
    # Tri-state mapping for the service: a field absent from the payload
    # stays None (leave unchanged); a field present — even as JSON null —
    # becomes a concrete string ("" clears, a value sets).
    seed = await service.update_names(
        character_id=character_id,
        operator_id=current_user_id,
        user_address_name=(
            (payload.user_address_name or "")
            if "user_address_name" in payload.model_fields_set
            else None
        ),
        character_address_name=(
            (payload.character_address_name or "")
            if "character_address_name" in payload.model_fields_set
            else None
        ),
    )
    # Invalidate the player-facing persona projection so the reconciled
    # name shows up immediately in "how she sees you".
    projection = getattr(container, "operator_persona_projection_service", None)
    if projection is not None:
        projection.invalidate(character_id, current_user_id)
    return RelationshipNamesResponse.from_seed(seed)
