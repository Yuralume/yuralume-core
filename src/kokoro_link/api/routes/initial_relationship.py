"""Player edit of the whole per-character initial relationship seed.

The seed is what the player filled in at character creation (關係、可知道
的背景、居住安排、稱呼、語氣距離、行程參與程度、主動訊息授權…). It used
to be write-once: only the two address names could be changed afterwards,
through ``/characters/{id}/relationship-names``. These two endpoints open
the rest of it for editing.

``confirmed_by_user`` is not editable here: it records that the player
confirmed the seed when the character was created, which a later edit has
no standing to re-assert.

The address names are part of the payload but are **not** written by this
route — ``RelationshipNamesService.update_seed`` delegates them to
``update_names`` so the rename-log event and the learned-persona
reconcile still happen. The narrower ``/relationship-names`` endpoints
stay exactly as they were.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, model_validator

from kokoro_link.api.dependencies import (
    ensure_character_id_owned_by_user,
    get_container,
    get_current_user_id,
)
from kokoro_link.bootstrap.container import ServiceContainer
from kokoro_link.domain.entities.character_operator_relationship_seed import (
    SCHEDULE_INVOLVEMENT_POLICIES,
    CharacterOperatorRelationshipSeed,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["initial-relationship"])

_MAX_LABEL_CHARS = 80
_MAX_TEXT_CHARS = 800
_MAX_NAME_CHARS = 80
_MAX_TONE_CHARS = 80
_MAX_CADENCE_CHARS = 160
_MAX_LIVING_ARRANGEMENT_CHARS = 240

# Every field this endpoint may write. ``confirmed_by_user`` is absent on
# purpose (see the module docstring); the timestamps are owned by the
# service.
_EDITABLE_FIELDS: tuple[str, ...] = (
    "relationship_label",
    "known_context",
    "living_arrangement",
    "user_address_name",
    "character_address_name",
    "tone_distance",
    "familiarity_boundary",
    "schedule_involvement_policy",
    "proactive_permission",
    "proactive_cadence_hint",
    "user_profile_notes",
)


class InitialRelationshipPatch(BaseModel):
    """Partial edit of the relationship seed.

    Tri-state per field: omit it to leave the stored value alone, send an
    empty string to clear it, send a value to set it. An empty
    ``schedule_involvement_policy`` means "back to ``none``".
    """

    relationship_label: str | None = Field(default=None, max_length=_MAX_LABEL_CHARS)
    known_context: str | None = Field(default=None, max_length=_MAX_TEXT_CHARS)
    living_arrangement: str | None = Field(
        default=None, max_length=_MAX_LIVING_ARRANGEMENT_CHARS,
    )
    user_address_name: str | None = Field(
        default=None,
        max_length=_MAX_NAME_CHARS,
        description="How the character should address the player.",
    )
    character_address_name: str | None = Field(
        default=None,
        max_length=_MAX_NAME_CHARS,
        description="How the player addresses the character.",
    )
    tone_distance: str | None = Field(default=None, max_length=_MAX_TONE_CHARS)
    familiarity_boundary: str | None = Field(default=None, max_length=_MAX_TEXT_CHARS)
    schedule_involvement_policy: str | None = Field(
        default=None,
        description=(
            "One of "
            f"{sorted(SCHEDULE_INVOLVEMENT_POLICIES)}; empty string reverts "
            "to 'none'."
        ),
    )
    proactive_permission: bool | None = None
    proactive_cadence_hint: str | None = Field(
        default=None, max_length=_MAX_CADENCE_CHARS,
    )
    user_profile_notes: str | None = Field(default=None, max_length=_MAX_TEXT_CHARS)

    @model_validator(mode="after")
    def _validate_schedule_policy(self) -> "InitialRelationshipPatch":
        """Reject an unknown policy at the API boundary (422) rather than
        letting the entity's ``ValueError`` escape as a 500. Mirrors the
        create-character payload's validator."""
        if "schedule_involvement_policy" not in self.model_fields_set:
            return self
        policy = (self.schedule_involvement_policy or "none").strip().lower()
        if policy not in SCHEDULE_INVOLVEMENT_POLICIES:
            raise ValueError(
                "schedule_involvement_policy must be one of "
                f"{sorted(SCHEDULE_INVOLVEMENT_POLICIES)}, got "
                f"{self.schedule_involvement_policy!r}",
            )
        self.schedule_involvement_policy = policy
        return self


class InitialRelationshipResponse(BaseModel):
    character_id: str
    operator_id: str
    has_seed: bool
    relationship_label: str = ""
    known_context: str = ""
    living_arrangement: str = ""
    user_address_name: str = ""
    character_address_name: str = ""
    tone_distance: str = ""
    familiarity_boundary: str = ""
    schedule_involvement_policy: str = "none"
    proactive_permission: bool = False
    proactive_cadence_hint: str = ""
    user_profile_notes: str = ""
    confirmed_by_user: bool = True

    @classmethod
    def from_seed(
        cls,
        seed: CharacterOperatorRelationshipSeed,
    ) -> "InitialRelationshipResponse":
        return cls(
            character_id=seed.character_id,
            operator_id=seed.operator_id,
            has_seed=True,
            relationship_label=seed.relationship_label,
            known_context=seed.known_context,
            living_arrangement=seed.living_arrangement,
            user_address_name=seed.user_address_name,
            character_address_name=seed.character_address_name,
            tone_distance=seed.tone_distance,
            familiarity_boundary=seed.familiarity_boundary,
            schedule_involvement_policy=seed.schedule_involvement_policy,
            proactive_permission=seed.proactive_permission,
            proactive_cadence_hint=seed.proactive_cadence_hint,
            user_profile_notes=seed.user_profile_notes,
            confirmed_by_user=seed.confirmed_by_user,
        )

    @classmethod
    def empty(
        cls, *, character_id: str, operator_id: str,
    ) -> "InitialRelationshipResponse":
        """Defaults for a pair that has no seed row, so the editor can
        pre-fill a blank form without special-casing 404."""
        return cls(
            character_id=character_id,
            operator_id=operator_id,
            has_seed=False,
        )


def _require_service(container: ServiceContainer):
    service = getattr(container, "relationship_names_service", None)
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="relationship seed service not wired",
        )
    return service


def _tri_state_text(payload: InitialRelationshipPatch, field: str) -> str | None:
    """``None`` when the field is absent (leave unchanged); otherwise a
    concrete string — JSON ``null`` collapses to ``""`` (clear), matching
    the relationship-names endpoint."""
    if field not in payload.model_fields_set:
        return None
    return getattr(payload, field) or ""


@router.get(
    "/characters/{character_id}/initial-relationship",
    response_model=InitialRelationshipResponse,
)
async def get_initial_relationship(
    character_id: str,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> InitialRelationshipResponse:
    """Return the caller's whole relationship seed for this character."""
    await ensure_character_id_owned_by_user(
        character_id,
        current_user_id,
        container,
    )
    service = _require_service(container)
    seed = await service.get_seed(
        character_id=character_id,
        operator_id=current_user_id,
    )
    if seed is None:
        return InitialRelationshipResponse.empty(
            character_id=character_id,
            operator_id=current_user_id,
        )
    return InitialRelationshipResponse.from_seed(seed)


@router.patch(
    "/characters/{character_id}/initial-relationship",
    response_model=InitialRelationshipResponse,
)
async def update_initial_relationship(
    character_id: str,
    payload: InitialRelationshipPatch,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> InitialRelationshipResponse:
    """Edit the relationship seed after creation.

    Scoped to the caller's own pair: ``character_id`` is ownership-checked
    and the seed is keyed to the caller's id, so nobody can rewrite
    someone else's relationship. Upserts — a pair with no seed row yet
    gets one.
    """
    if not (set(_EDITABLE_FIELDS) & payload.model_fields_set):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "provide at least one of: " + ", ".join(_EDITABLE_FIELDS)
            ),
        )
    await ensure_character_id_owned_by_user(
        character_id,
        current_user_id,
        container,
    )
    service = _require_service(container)
    try:
        seed = await service.update_seed(
            character_id=character_id,
            operator_id=current_user_id,
            relationship_label=_tri_state_text(payload, "relationship_label"),
            known_context=_tri_state_text(payload, "known_context"),
            living_arrangement=_tri_state_text(payload, "living_arrangement"),
            user_address_name=_tri_state_text(payload, "user_address_name"),
            character_address_name=_tri_state_text(
                payload, "character_address_name",
            ),
            tone_distance=_tri_state_text(payload, "tone_distance"),
            familiarity_boundary=_tri_state_text(payload, "familiarity_boundary"),
            schedule_involvement_policy=_tri_state_text(
                payload, "schedule_involvement_policy",
            ),
            proactive_permission=(
                bool(payload.proactive_permission)
                if "proactive_permission" in payload.model_fields_set
                else None
            ),
            proactive_cadence_hint=_tri_state_text(
                payload, "proactive_cadence_hint",
            ),
            user_profile_notes=_tri_state_text(payload, "user_profile_notes"),
        )
    except ValueError as exc:
        # Domain-level rejection (an unknown schedule policy that slipped
        # past the payload validator, an empty id) is a bad request from
        # the client, not a server fault.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    # Keep "how she sees you" in step with the reconciled address name.
    projection = getattr(container, "operator_persona_projection_service", None)
    if projection is not None:
        projection.invalidate(character_id, current_user_id)
    return InitialRelationshipResponse.from_seed(seed)
