"""Player-declared persona note — per character.

``GET  /characters/{character_id}/player-persona-note`` — read the
current declaration so the editor can pre-fill.
``PUT  /characters/{character_id}/player-persona-note`` — write it, or
clear it by sending an empty string.

Scoped to the caller's own pair: ``character_id`` is ownership-checked
and the row is keyed to the caller's id, so a Bob cannot read or rewrite
the setting Alice declared to her character.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from kokoro_link.api.dependencies import (
    ensure_character_id_owned_by_user,
    get_container,
    get_current_user_id,
)
from kokoro_link.application.services.player_persona_note_service import (
    PlayerPersonaNoteService,
)
from kokoro_link.bootstrap.container import ServiceContainer
from kokoro_link.domain.entities.player_persona_note import (
    PLAYER_PERSONA_NOTE_MAX_CHARS,
    PlayerPersonaNote,
)

router = APIRouter(tags=["player-persona-note"])


class PlayerPersonaNotePut(BaseModel):
    """Full replacement. An empty string clears the declaration."""

    note: str = Field(
        default="",
        max_length=PLAYER_PERSONA_NOTE_MAX_CHARS,
        description=(
            "The player's own identity / world premise for this "
            "relationship. Empty string clears it."
        ),
    )


class PlayerPersonaNoteResponse(BaseModel):
    character_id: str
    operator_id: str
    note: str
    updated_at: datetime | None = None

    @classmethod
    def from_domain(
        cls,
        note: PlayerPersonaNote | None,
        *,
        character_id: str,
        operator_id: str,
    ) -> "PlayerPersonaNoteResponse":
        if note is None:
            return cls(
                character_id=character_id,
                operator_id=operator_id,
                note="",
            )
        return cls(
            character_id=note.character_id,
            operator_id=note.operator_id,
            note=note.note,
            updated_at=note.updated_at,
        )


def _service(container: ServiceContainer) -> PlayerPersonaNoteService:
    service = getattr(container, "player_persona_note_service", None)
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="player persona note service not wired",
        )
    return service


@router.get(
    "/characters/{character_id}/player-persona-note",
    response_model=PlayerPersonaNoteResponse,
)
async def get_player_persona_note(
    character_id: str,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> PlayerPersonaNoteResponse:
    """Return the declaration for this pair; empty note when unset."""
    await ensure_character_id_owned_by_user(
        character_id,
        current_user_id,
        container,
    )
    note = await _service(container).get(
        character_id=character_id,
        operator_id=current_user_id,
    )
    return PlayerPersonaNoteResponse.from_domain(
        note,
        character_id=character_id,
        operator_id=current_user_id,
    )


@router.put(
    "/characters/{character_id}/player-persona-note",
    response_model=PlayerPersonaNoteResponse,
)
async def put_player_persona_note(
    character_id: str,
    payload: PlayerPersonaNotePut,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> PlayerPersonaNoteResponse:
    """Write the declaration, or clear it with an empty string.

    The length ceiling is enforced here (422) *and* in the entity — a
    front end is not a validation layer, and this text is injected into
    every prompt of the relationship.
    """
    await ensure_character_id_owned_by_user(
        character_id,
        current_user_id,
        container,
    )
    try:
        note = await _service(container).set(
            character_id=character_id,
            operator_id=current_user_id,
            note=payload.note,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from None
    return PlayerPersonaNoteResponse.from_domain(
        note,
        character_id=character_id,
        operator_id=current_user_id,
    )
