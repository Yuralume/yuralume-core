"""玩家身分卡 — the player's reusable character-creation templates.

``GET    /identity-cards``        — every card this player owns.
``POST   /identity-cards``        — save one; ``overwrite`` replaces a
                                    same-named card in place.
``PATCH  /identity-cards/{id}``   — rename.
``DELETE /identity-cards/{id}``   — remove.

Account-level, not per character: the collection hangs off the caller's
session rather than a character path, and every id is resolved *with* the
caller's operator id, so another player's card id reads as 404 rather
than as a permission error. There is no apply endpoint — a card is copied
into the creation wizard client-side and travels the existing character
and persona-note paths from there (PLAYER_IDENTITY_CARD_PLAN §2.2).
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, model_validator

from kokoro_link.api.dependencies import get_container, get_current_user_id
from kokoro_link.application.services.player_identity_card_service import (
    PlayerIdentityCardLimitReachedError,
    PlayerIdentityCardNameConflictError,
    PlayerIdentityCardService,
)
from kokoro_link.bootstrap.container import ServiceContainer
from kokoro_link.domain.entities.character_operator_relationship_seed import (
    SCHEDULE_INVOLVEMENT_POLICIES,
)
from kokoro_link.domain.entities.player_identity_card import (
    PLAYER_IDENTITY_CARD_CONTENT_FIELDS,
    PLAYER_IDENTITY_CARD_NAME_MAX_CHARS,
    PlayerIdentityCard,
)
from kokoro_link.domain.entities.player_persona_note import (
    PLAYER_PERSONA_NOTE_MAX_CHARS,
)

router = APIRouter(tags=["player-identity-card"])

NAME_CONFLICT_CODE = "identity_card_name_conflict"
LIMIT_REACHED_CODE = "identity_card_limit_reached"
NOT_FOUND_CODE = "identity_card_not_found"


class _IdentityCardContent(BaseModel):
    """The eleven creation-intake fields plus the player's persona note.

    The nine free-text seed fields carry **no** ``max_length`` on
    purpose. Their ceilings live in ``SEED_TEXT_FIELD_MAX_CHARS`` (see
    ``domain.entities.character_operator_relationship_seed``) and are
    applied by :class:`PlayerIdentityCard` as a *clip* via
    ``trim_seed_text`` — the seed's long-standing contract: character
    creation accepts an over-long intake answer and stores its first N
    characters. A card is a copy of that same intake, so rejecting here
    what the creation wizard silently accepts would make a card
    unsaveable for exactly the wizard answer it is supposed to remember
    — the player fills the form, creates the character fine, then gets a
    422 when they tick "save this as a card".

    ``persona_note`` is the deliberate exception: ``PlayerPersonaNote``
    rejects rather than clips, so a card that quietly stored half a
    world premise would write that half into every character made from
    it. ``name`` (on the create/rename requests) is likewise a reject —
    a silently halved label is a card the player cannot find again.
    """

    relationship_label: str = ""
    known_context: str = ""
    living_arrangement: str = ""
    user_address_name: str = Field(
        default="",
        description="How the character should address the player.",
    )
    character_address_name: str = Field(
        default="",
        description="How the player addresses the character.",
    )
    tone_distance: str = ""
    familiarity_boundary: str = ""
    schedule_involvement_policy: str = Field(
        default="none",
        description=f"One of {sorted(SCHEDULE_INVOLVEMENT_POLICIES)}.",
    )
    proactive_permission: bool = False
    proactive_cadence_hint: str = ""
    user_profile_notes: str = ""
    persona_note: str = Field(
        default="",
        max_length=PLAYER_PERSONA_NOTE_MAX_CHARS,
        description="The player's own identity / world premise (PP series).",
    )

    def content(self) -> dict[str, object]:
        return {
            field: getattr(self, field)
            for field in PLAYER_IDENTITY_CARD_CONTENT_FIELDS
        }


class IdentityCardCreateRequest(_IdentityCardContent):
    name: str = Field(
        min_length=1,
        max_length=PLAYER_IDENTITY_CARD_NAME_MAX_CHARS,
        description="Player-chosen label, unique within the account.",
    )
    overwrite: bool = Field(
        default=False,
        description=(
            "When a card with this name already exists: false → 409 with "
            f"code '{NAME_CONFLICT_CODE}'; true → replace its content in "
            "place, keeping the original id."
        ),
    )

    @model_validator(mode="after")
    def _validate(self) -> "IdentityCardCreateRequest":
        """Reject a blank-after-trim name and an unknown policy at the
        boundary (422), rather than letting the entity's ``ValueError``
        surface as a 500."""
        if not self.name.strip():
            raise ValueError("name must not be blank")
        policy = (self.schedule_involvement_policy or "none").strip().lower()
        if policy not in SCHEDULE_INVOLVEMENT_POLICIES:
            raise ValueError(
                "schedule_involvement_policy must be one of "
                f"{sorted(SCHEDULE_INVOLVEMENT_POLICIES)}, got "
                f"{self.schedule_involvement_policy!r}",
            )
        self.schedule_involvement_policy = policy
        return self


class IdentityCardRenameRequest(BaseModel):
    """Rename only. Content edits go through a same-name overwrite save."""

    name: str = Field(
        min_length=1, max_length=PLAYER_IDENTITY_CARD_NAME_MAX_CHARS,
    )

    @model_validator(mode="after")
    def _validate(self) -> "IdentityCardRenameRequest":
        if not self.name.strip():
            raise ValueError("name must not be blank")
        return self


class IdentityCardResponse(_IdentityCardContent):
    id: str
    operator_id: str
    name: str
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @classmethod
    def from_domain(cls, card: PlayerIdentityCard) -> "IdentityCardResponse":
        return cls(
            id=card.id,
            operator_id=card.operator_id,
            name=card.name,
            created_at=card.created_at,
            updated_at=card.updated_at,
            **{
                field: getattr(card, field)
                for field in PLAYER_IDENTITY_CARD_CONTENT_FIELDS
            },
        )


class IdentityCardListResponse(BaseModel):
    cards: list[IdentityCardResponse]
    limit: int = Field(
        description="Per-account cap, so the UI can warn before the 409.",
    )


def _service(container: ServiceContainer) -> PlayerIdentityCardService:
    service = getattr(container, "player_identity_card_service", None)
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="player identity card service not wired",
        )
    return service


def _conflict(error: PlayerIdentityCardNameConflictError) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "code": NAME_CONFLICT_CODE,
            "card_id": error.existing.id,
            "name": error.existing.name,
        },
    )


def _not_found() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"code": NOT_FOUND_CODE},
    )


@router.get("/identity-cards", response_model=IdentityCardListResponse)
async def list_identity_cards(
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> IdentityCardListResponse:
    """Every card the caller owns, full content included.

    The picker pre-fills the wizard straight from this response, so a
    thinner list DTO would only buy a second request per selection.
    """
    service = _service(container)
    cards = await service.list_cards(current_user_id)
    return IdentityCardListResponse(
        cards=[IdentityCardResponse.from_domain(card) for card in cards],
        limit=service.limit,
    )


@router.post(
    "/identity-cards",
    response_model=IdentityCardResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_identity_card(
    payload: IdentityCardCreateRequest,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> IdentityCardResponse:
    """Save a card, or overwrite the same-named one with ``overwrite``."""
    try:
        card = await _service(container).save_card(
            operator_id=current_user_id,
            name=payload.name,
            overwrite=payload.overwrite,
            **payload.content(),
        )
    except PlayerIdentityCardNameConflictError as exc:
        raise _conflict(exc) from exc
    except PlayerIdentityCardLimitReachedError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": LIMIT_REACHED_CODE,
                "current": exc.current,
                "limit": exc.limit,
            },
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc),
        ) from exc
    return IdentityCardResponse.from_domain(card)


@router.patch(
    "/identity-cards/{card_id}", response_model=IdentityCardResponse,
)
async def rename_identity_card(
    card_id: str,
    payload: IdentityCardRenameRequest,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> IdentityCardResponse:
    """Rename one of the caller's cards. Another owner's id is a 404."""
    try:
        card = await _service(container).rename_card(
            card_id=card_id,
            operator_id=current_user_id,
            name=payload.name,
        )
    except PlayerIdentityCardNameConflictError as exc:
        raise _conflict(exc) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc),
        ) from exc
    if card is None:
        raise _not_found()
    return IdentityCardResponse.from_domain(card)


@router.delete(
    "/identity-cards/{card_id}", status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_identity_card(
    card_id: str,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> None:
    """Delete one of the caller's cards.

    Characters already created from it are untouched — applying a card
    copies values rather than linking to them.
    """
    deleted = await _service(container).delete_card(
        card_id=card_id, operator_id=current_user_id,
    )
    if not deleted:
        raise _not_found()
