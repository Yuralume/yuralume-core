"""Operator-persona admin endpoints — **per character**.

Read-mostly inspection surface for the five-layer persona accumulation
that each character independently builds about the operator.

- ``GET  /operator/persona?character_id=<id>`` — snapshot of confirmed
  fields by layer + Layer-4 interaction strength + pending candidates
  with evidence, for one character's view of the operator
- ``POST /operator/persona/candidates/{id}/reject`` — manual override to
  drop a hallucinated pending row before the dream job sees it
- ``POST /operator/persona/fields/{id}/state`` — manual override on a
  confirmed row (e.g. mark stale / superseded after the operator
  notices a wrong fact)
- ``POST /admin/operator/persona/dream-tick?character_id=<id>`` — run
  one dream pass for this character right now, ignoring the quiet-
  hours / interval gate. Useful for "I just said something
  interesting, promote it without waiting for tonight".

Single-operator app today; everything pivots on ``DEFAULT_OPERATOR_ID``.
``character_id`` is REQUIRED — there's no operator-global persona
since the per-character pivot, so a missing character_id is a 400.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from kokoro_link.api.dependencies import (
    ensure_character_id_owned_by_user,
    get_container,
    get_current_user_id,
    require_admin,
)
from kokoro_link.application.dto.operator_persona_projection import (
    PersonaProjectionResponse,
)
from kokoro_link.application.services.operator_persona_projection_service import (
    OperatorPersonaProjectionCharacterNotFoundError,
)
from kokoro_link.bootstrap.container import ServiceContainer
from kokoro_link.domain.entities.character_operator_relationship_seed import (
    CharacterOperatorRelationshipSeed,
)
from kokoro_link.domain.entities.operator_persona import (
    InteractionStrength,
    OperatorPersona,
)
from kokoro_link.domain.entities.operator_profile import DEFAULT_OPERATOR_ID
from kokoro_link.domain.value_objects.profile_field import (
    CandidateField,
    EvidenceRef,
    ProfileField,
)
from kokoro_link.infrastructure.prompt.initial_relationship import (
    render_initial_relationship_seed_lines,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["operator-persona"])


# ---- Response models ---------------------------------------------------------


class EvidenceRefResponse(BaseModel):
    turn_id: str
    conversation_id: str
    quote: str
    extracted_at: datetime

    @classmethod
    def from_domain(cls, ev: EvidenceRef) -> "EvidenceRefResponse":
        return cls(
            turn_id=ev.turn_id,
            conversation_id=ev.conversation_id,
            quote=ev.quote,
            extracted_at=ev.extracted_at,
        )


class ProfileFieldResponse(BaseModel):
    field_id: str | None
    layer: int
    field_key: str
    value: str
    confidence: float
    source: str
    update_count: int
    last_updated: datetime
    evidence: list[EvidenceRefResponse]

    @classmethod
    def from_domain(cls, fld: ProfileField) -> "ProfileFieldResponse":
        return cls(
            field_id=fld.field_id,
            layer=fld.layer,
            field_key=fld.field_key,
            value=fld.value,
            confidence=fld.confidence,
            source=fld.source,
            update_count=fld.update_count,
            last_updated=fld.last_updated,
            evidence=[
                EvidenceRefResponse.from_domain(ev) for ev in fld.evidence_refs
            ],
        )


class CandidateFieldResponse(BaseModel):
    candidate_id: str | None
    layer: int
    field_key: str
    proposed_value: str
    raw_extractor_confidence: float
    state: str
    source: str
    explicit: bool
    extracted_at: datetime
    evidence: EvidenceRefResponse

    @classmethod
    def from_domain(cls, c: CandidateField) -> "CandidateFieldResponse":
        return cls(
            candidate_id=c.candidate_id,
            layer=c.layer,
            field_key=c.field_key,
            proposed_value=c.proposed_value,
            raw_extractor_confidence=c.raw_extractor_confidence,
            state=c.state,
            source=c.source,
            explicit=c.explicit,
            extracted_at=c.extracted_at,
            evidence=EvidenceRefResponse.from_domain(c.evidence_ref),
        )


class InteractionStrengthResponse(BaseModel):
    first_message_at: datetime | None
    total_user_messages: int
    days_since_first_contact: int
    messages_last_7_days: int
    messages_last_30_days: int
    longest_session_minutes: int
    shared_arc_realized_count: int
    shared_drama_count: int
    familiarity_band: str
    computed_at: datetime

    @classmethod
    def from_domain(cls, s: InteractionStrength) -> "InteractionStrengthResponse":
        return cls(
            first_message_at=s.first_message_at,
            total_user_messages=s.total_user_messages,
            days_since_first_contact=s.days_since_first_contact,
            messages_last_7_days=s.messages_last_7_days,
            messages_last_30_days=s.messages_last_30_days,
            longest_session_minutes=s.longest_session_minutes,
            shared_arc_realized_count=s.shared_arc_realized_count,
            shared_drama_count=s.shared_drama_count,
            familiarity_band=s.familiarity_band.value,
            computed_at=s.computed_at,
        )


class InitialRelationshipResponse(BaseModel):
    relationship_label: str
    summary_lines: list[str] = Field(default_factory=list)

    @classmethod
    def from_domain(
        cls,
        seed: CharacterOperatorRelationshipSeed,
    ) -> "InitialRelationshipResponse | None":
        if seed.is_empty:
            return None
        return cls(
            relationship_label=seed.relationship_label.strip(),
            summary_lines=render_initial_relationship_seed_lines(seed),
        )


class PersonaSnapshotResponse(BaseModel):
    character_id: str
    operator_id: str
    layer1_identity: list[ProfileFieldResponse]
    layer2_life: list[ProfileFieldResponse]
    layer3_emotional: list[ProfileFieldResponse]
    layer5_trust: list[ProfileFieldResponse]
    interaction_strength: InteractionStrengthResponse | None
    initial_relationship: InitialRelationshipResponse | None = None
    pending_candidates: list[CandidateFieldResponse]
    prompt_preview_lines: list[str] = Field(
        default_factory=list,
        description=(
            "The exact Chinese lines OperatorPersonaService would splice "
            "into the chat prompt. Lets the operator see what the model "
            "will actually be told, without diffing two prompts manually."
        ),
    )

    @classmethod
    def from_domain(
        cls,
        persona: OperatorPersona,
        *,
        prompt_preview_lines: list[str],
        initial_relationship: InitialRelationshipResponse | None = None,
    ) -> "PersonaSnapshotResponse":
        return cls(
            character_id=persona.character_id,
            operator_id=persona.operator_id,
            layer1_identity=_dump_layer(persona.layer1_identity),
            layer2_life=_dump_layer(persona.layer2_life),
            layer3_emotional=_dump_layer(persona.layer3_emotional),
            layer5_trust=_dump_layer(persona.layer5_trust),
            interaction_strength=(
                InteractionStrengthResponse.from_domain(persona.layer4_interaction)
                if persona.layer4_interaction is not None
                else None
            ),
            initial_relationship=initial_relationship,
            pending_candidates=[
                CandidateFieldResponse.from_domain(c)
                for c in persona.pending_candidates
            ],
            prompt_preview_lines=prompt_preview_lines,
        )


def _dump_layer(fields) -> list[ProfileFieldResponse]:
    """Sorted by confidence descending so the most-trusted facts surface
    first in the UI."""
    return sorted(
        (ProfileFieldResponse.from_domain(f) for f in fields.values()),
        key=lambda r: (-r.confidence, r.field_key),
    )


# ---- Mutation models --------------------------------------------------------


_ALLOWED_FIELD_STATES = {"stale", "superseded", "rejected"}


class MarkStateRequest(BaseModel):
    state: str = Field(
        description="Target state for the row.",
        examples=["rejected", "stale", "superseded"],
    )


class SetPersonaFieldRequest(BaseModel):
    character_id: str = Field(
        description="Which character's view of the operator to correct.",
    )
    field_key: str = Field(
        description="Editable identity field (name / nickname).",
        examples=["name", "nickname"],
    )
    value: str = Field(
        description="The corrected value the character should use.",
        min_length=1,
    )


class DreamTickResponse(BaseModel):
    applied: bool
    promotions: int
    merges: int
    supersedes: int
    rejections: int
    decays: int
    inferences: int


# ---- Endpoints --------------------------------------------------------------


@router.get(
    "/operator/persona",
    response_model=PersonaSnapshotResponse,
)
async def get_operator_persona(
    character_id: str,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> PersonaSnapshotResponse:
    """Return one character's view of the operator persona.

    ``character_id`` is required — per-character is the whole point;
    there's no operator-global snapshot to fall back to. Layer 4 is
    recomputed on every call (with the service's 60s in-memory cache
    absorbing the cost). Confirmed fields are sorted by confidence
    descending so the UI can render highest-trust facts first; pending
    candidates are returned oldest-first to match the order the dream
    job would consume them in.

    Multi-user note: the prior external ``operator_id`` query argument
    is gone — the scope is locked to the caller's user id so a Bob can
    no longer fetch Alice's persona by guessing her id. The character id
    itself is also owner-checked before loading persona rows, so arbitrary
    foreign character ids collapse to 404 instead of returning an empty
    persona shell.
    """
    await ensure_character_id_owned_by_user(
        character_id,
        current_user_id,
        container,
    )
    service = container.operator_persona_service
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="operator persona service not wired",
        )
    persona = await service.get_current(character_id, current_user_id)
    preview = service.render_for_prompt(persona)
    initial_relationship = await _load_initial_relationship_response(
        container,
        character_id=character_id,
        operator_id=current_user_id,
    )
    return PersonaSnapshotResponse.from_domain(
        persona,
        prompt_preview_lines=preview,
        initial_relationship=initial_relationship,
    )


async def _load_initial_relationship_response(
    container: ServiceContainer,
    *,
    character_id: str,
    operator_id: str,
) -> InitialRelationshipResponse | None:
    repository = getattr(container, "relationship_seed_repository", None)
    if repository is None:
        return None
    try:
        seed = await repository.get(character_id, operator_id)
    except Exception:
        logger.exception(
            "operator persona initial relationship seed lookup failed",
            extra={"character_id": character_id, "operator_id": operator_id},
        )
        return None
    if seed is None:
        return None
    return InitialRelationshipResponse.from_domain(seed)


@router.get(
    "/operator/persona/projection",
    response_model=PersonaProjectionResponse,
)
async def get_operator_persona_projection(
    character_id: str,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> PersonaProjectionResponse:
    """Return the player-safe "how she sees you" projection.

    Unlike the raw debug snapshot, this endpoint never returns evidence,
    confidence, pending rows, Layer 3 emotional inferences, or Layer 5
    trust/dependence data. The application service reads the scoped
    aggregate and performs the final LLM narrative projection.
    """
    await ensure_character_id_owned_by_user(
        character_id,
        current_user_id,
        container,
    )
    service = getattr(container, "operator_persona_projection_service", None)
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="operator persona projection service not wired",
        )
    try:
        return await service.project(character_id, operator_id=current_user_id)
    except OperatorPersonaProjectionCharacterNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Character not found",
        ) from None


@router.post(
    "/operator/persona/candidates/{candidate_id}/reject",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def reject_pending_candidate(
    candidate_id: str,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> None:
    """Manually drop a pending candidate.

    Useful when the operator spots a hallucinated row before the next
    dream tick (e.g. extractor decided the user "lives in Tokyo" from
    a movie reference). After this the dream job won't reconsider it.

    Ownership: the candidate must belong to the calling operator. A row
    owned by someone else (or a non-existent id) collapses to 404 — the
    persona is per-(character, operator) and a Bob must not be able to
    veto Alice's staging rows by guessing candidate ids.
    """
    service = container.operator_persona_service
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="operator persona service not wired",
        )
    character_id = await _ensure_persona_row_owned(
        row_id=candidate_id,
        service=service,
        container=container,
        current_user_id=current_user_id,
        detail="Persona candidate not found",
    )
    ok = await service.reject_candidate_for_operator(
        candidate_id, current_user_id,
    )
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Persona candidate not found",
        )
    _invalidate_projection(container, character_id, current_user_id)


@router.post(
    "/operator/persona/fields/{field_id}/state",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def transition_field_state(
    field_id: str,
    payload: MarkStateRequest,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> None:
    """Manually move a confirmed field into a terminal state.

    Allowed targets: ``stale`` (hide from prompt, keep history),
    ``superseded`` (replaced by a newer fact), ``rejected`` (manual
    veto). Promotion back to ``confirmed`` is intentionally not
    exposed — re-confirmation should go through the normal extraction
    + dream pipeline so evidence is auditable.

    Ownership: the field must belong to the calling operator. A row
    owned by someone else (or a non-existent id) collapses to 404, same
    as :func:`reject_pending_candidate`.
    """
    state = payload.state.strip().lower()
    if state not in _ALLOWED_FIELD_STATES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"state must be one of {sorted(_ALLOWED_FIELD_STATES)}",
        )
    service = container.operator_persona_service
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="operator persona service not wired",
        )
    character_id = await _ensure_persona_row_owned(
        row_id=field_id,
        service=service,
        container=container,
        current_user_id=current_user_id,
        detail="Persona field not found",
    )
    ok = await service.transition_field_state_for_operator(
        field_id, state, current_user_id,
    )
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Persona field not found",
        )
    _invalidate_projection(container, character_id, current_user_id)


@router.put(
    "/operator/persona/fields",
    response_model=ProfileFieldResponse,
)
async def set_persona_field(
    payload: SetPersonaFieldRequest,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> ProfileFieldResponse:
    """Player correction of how this character addresses the operator.

    Writes an explicit ``name`` / ``nickname`` override for one
    character's view of the operator, superseding any learned value
    (the service stamps the old confirmed row ``superseded`` first so
    history is preserved). Per-character: the correction never leaks to
    sibling characters or the global profile. A non-editable field key
    or empty value is a 400.

    Ownership: ``character_id`` must belong to the calling operator, and
    the persona row is keyed to the caller's own id — a Bob can't edit
    Alice's persona.
    """
    await ensure_character_id_owned_by_user(
        payload.character_id,
        current_user_id,
        container,
    )
    service = container.operator_persona_service
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="operator persona service not wired",
        )
    try:
        field = await service.set_explicit_field_for_operator(
            character_id=payload.character_id,
            operator_id=current_user_id,
            field_key=payload.field_key,
            value=payload.value,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from None
    _invalidate_projection(container, payload.character_id, current_user_id)
    return ProfileFieldResponse.from_domain(field)


async def _ensure_persona_row_owned(
    *,
    row_id: str,
    service,
    container: ServiceContainer,
    current_user_id: str,
    detail: str,
) -> str:
    scope = await service.get_row_scope(row_id)
    if scope is None or scope[1] != current_user_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=detail,
        )
    character_id, _operator_id = scope
    await ensure_character_id_owned_by_user(
        character_id,
        current_user_id,
        container,
    )
    return character_id


def _invalidate_projection(
    container: ServiceContainer,
    character_id: str,
    operator_id: str,
) -> None:
    service = getattr(container, "operator_persona_projection_service", None)
    if service is not None:
        service.invalidate(character_id, operator_id)


@router.post(
    "/admin/operator/persona/dream-tick",
    response_model=DreamTickResponse,
)
async def trigger_dream_tick(
    character_id: str,
    container: ServiceContainer = Depends(get_container),
    admin=Depends(require_admin),
) -> DreamTickResponse:
    """Force one dream pass for ``character_id`` right now, ignoring
    the quiet-hours / pending-count / min-interval gate.

    Mirrors ``/admin/pending-follow-ups/tick`` — handy for "I just
    revealed three facts in a single chat, promote them now" instead
    of waiting until 23:00. The consolidator LLM still applies its
    own guards (Layer 3 first-person check, layer-specific confidence
    caps); this endpoint just bypasses the *scheduling* gate, not the
    *correctness* one.

    Multi-user note: the previous ``operator_id`` query argument is gone
    — admins can no longer point the dream-tick at another user's
    persona. The persona scope is fixed to the admin's own identity to
    avoid leaking cross-user data through the consolidation log.
    """
    service = container.persona_dream_service
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="persona dream service not wired",
        )
    plan = await service.run_consolidation(
        character_id, admin.id, now=datetime.now(timezone.utc),
    )
    return DreamTickResponse(
        applied=not plan.is_empty(),
        promotions=len(plan.promotions),
        merges=len(plan.merges),
        supersedes=len(plan.supersedes),
        rejections=len(plan.rejections),
        decays=len(plan.decays),
        inferences=len(plan.inferences),
    )
