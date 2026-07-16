"""Arc-template wizard REST routes (Phase 2.7 of SCENE_BEAT_PLAN).

Each endpoint corresponds to a wizard step:

- ``POST /arc-templates/intake/suggest-meta`` — Stage 1
- ``POST /arc-templates/intake/condense-premise`` — Stage 2
- ``POST /arc-templates/intake/suggest-beat-options`` — Stage 4 (per beat)
- ``POST /arc-templates/intake/generate-summary`` — Stage 4 (per beat)
- ``POST /arc-templates/intake/generate-full-draft`` — fast-path
- ``POST /arc-templates`` — final save
- ``GET  /arc-templates/scaffolds`` — rhythm patterns + tone catalogue

Wizard state lives on the client; these endpoints are stateless.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from kokoro_link.api.dependencies import get_container, get_current_user_id
from kokoro_link.api.operator_language import (
    resolve_stored_operator_primary_language,
)
from kokoro_link.api.routes.arc_templates import ArcTemplateResponse
from kokoro_link.application.services.arc_template_intake_service import (
    ArcTemplateIntakeService,
    BeatContext,
    BeatDraft,
    TemplateDraft,
)
from kokoro_link.bootstrap.container import ServiceContainer
from kokoro_link.domain.entities.arc_template import ARC_TEMPLATE_SCOPE_GENERIC

router = APIRouter(tags=["arc-template-intake"])

_LOGGER = logging.getLogger(__name__)


def _require_intake_service(container: ServiceContainer) -> ArcTemplateIntakeService:
    if container.arc_template_intake_service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Arc template intake service not configured",
        )
    return container.arc_template_intake_service


async def _assert_target_characters_visible(
    container: ServiceContainer,
    *,
    character_ids: list[str],
    current_user_id: str,
) -> None:
    character_service = getattr(container, "character_service", None)
    if character_service is None:
        return
    seen: set[str] = set()
    for character_id in character_ids:
        cleaned = (character_id or "").strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        character = await character_service.get_character_entity(
            cleaned, user_id=current_user_id,
        )
        if character is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Character {cleaned!r} not found",
            )


# ---------- Stage 1: meta suggestions ----------------------------------


class SuggestMetaRequest(BaseModel):
    pitch: str = Field(..., min_length=1)


class SuggestMetaResponse(BaseModel):
    titles: list[str]
    themes: list[str]
    tones: list[str]
    world_frames: list[str]


@router.post(
    "/arc-templates/intake/suggest-meta",
    response_model=SuggestMetaResponse,
    summary="Stage 1：依使用者一句話 pitch 提案 title/theme/tone/world_frames",
)
async def suggest_meta(
    payload: SuggestMetaRequest,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> SuggestMetaResponse:
    service = _require_intake_service(container)
    language = await resolve_stored_operator_primary_language(
        container, current_user_id,
    )
    result = await service.suggest_meta(
        payload.pitch, operator_primary_language=language,
    )
    return SuggestMetaResponse(
        titles=list(result.titles),
        themes=list(result.themes),
        tones=list(result.tones),
        world_frames=list(result.world_frames),
    )


# ---------- Stage 2: premise condensation ------------------------------


class CondensePremiseRequest(BaseModel):
    logline: str = Field(..., min_length=1)
    start_state: str = ""
    end_state: str = ""
    tone: str = "daily"


class CondensePremiseResponse(BaseModel):
    premise: str


@router.post(
    "/arc-templates/intake/condense-premise",
    response_model=CondensePremiseResponse,
    summary="Stage 2：壓 logline + 起點 + 終點為 60–120 字 premise",
)
async def condense_premise(
    payload: CondensePremiseRequest,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> CondensePremiseResponse:
    service = _require_intake_service(container)
    language = await resolve_stored_operator_primary_language(
        container, current_user_id,
    )
    premise = await service.condense_premise(
        logline=payload.logline,
        start_state=payload.start_state,
        end_state=payload.end_state,
        tone=payload.tone,
        operator_primary_language=language,
    )
    return CondensePremiseResponse(premise=premise)


# ---------- Stage 4: per-beat suggestions + summary --------------------


class BeatContextPayload(BaseModel):
    template_title: str
    premise: str
    theme: str
    tone: str = "daily"
    duration_days: int = Field(default=14, ge=1, le=365)
    world_frames: list[str] = Field(default_factory=list)
    beat_position: int = Field(..., ge=0)
    total_beats: int = Field(..., ge=1)
    day_offset: int = Field(..., ge=0)
    tension: str = "rising"
    prior_titles: list[str] = Field(default_factory=list)

    def to_domain(self) -> BeatContext:
        return BeatContext(
            template_title=self.template_title,
            premise=self.premise,
            theme=self.theme,
            tone=self.tone,
            duration_days=self.duration_days,
            world_frames=tuple(self.world_frames),
            beat_position=self.beat_position,
            total_beats=self.total_beats,
            day_offset=self.day_offset,
            tension=self.tension,
            prior_titles=tuple(self.prior_titles),
        )


class SuggestBeatOptionsRequest(BaseModel):
    context: BeatContextPayload


class SuggestBeatOptionsResponse(BaseModel):
    titles: list[str]
    locations: list[str]
    scene_characters: list[str]
    dramatic_questions: list[str]
    scene_types: list[str]


@router.post(
    "/arc-templates/intake/suggest-beat-options",
    response_model=SuggestBeatOptionsResponse,
    summary="Stage 4：對單一 beat 提案 title/location/NPCs/question/scene_type 候選",
)
async def suggest_beat_options(
    payload: SuggestBeatOptionsRequest,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> SuggestBeatOptionsResponse:
    service = _require_intake_service(container)
    language = await resolve_stored_operator_primary_language(
        container, current_user_id,
    )
    result = await service.suggest_beat_options(
        payload.context.to_domain(), operator_primary_language=language,
    )
    return SuggestBeatOptionsResponse(
        titles=list(result.titles),
        locations=list(result.locations),
        scene_characters=list(result.scene_characters),
        dramatic_questions=list(result.dramatic_questions),
        scene_types=list(result.scene_types),
    )


class BeatDraftPayload(BaseModel):
    sequence: int = Field(..., ge=0)
    day_offset: int = Field(..., ge=0)
    title: str = ""
    summary: str = ""
    tension: str = "rising"
    scene_type: str = "encounter"
    location: str | None = None
    scene_characters: list[str] = Field(default_factory=list)
    dramatic_question: str | None = None
    required: bool = True

    def to_domain(self) -> BeatDraft:
        return BeatDraft(
            sequence=self.sequence,
            day_offset=self.day_offset,
            title=self.title,
            summary=self.summary,
            tension=self.tension,
            scene_type=self.scene_type,
            location=self.location,
            scene_characters=tuple(self.scene_characters),
            dramatic_question=self.dramatic_question,
            required=self.required,
        )


class GenerateSummaryRequest(BaseModel):
    beat: BeatDraftPayload
    context: BeatContextPayload


class GenerateSummaryResponse(BaseModel):
    summary: str


@router.post(
    "/arc-templates/intake/generate-summary",
    response_model=GenerateSummaryResponse,
    summary="Stage 4：依 beat 結構寫 100–150 字 summary",
)
async def generate_summary(
    payload: GenerateSummaryRequest,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> GenerateSummaryResponse:
    service = _require_intake_service(container)
    language = await resolve_stored_operator_primary_language(
        container, current_user_id,
    )
    summary = await service.generate_beat_summary(
        beat=payload.beat.to_domain(),
        context=payload.context.to_domain(),
        operator_primary_language=language,
    )
    return GenerateSummaryResponse(summary=summary)


# ---------- Fast-path: full draft --------------------------------------


class GenerateFullDraftRequest(BaseModel):
    pitch: str = Field(..., min_length=1)
    hint: str = ""


class TemplateDraftPayload(BaseModel):
    """Mirror of ``TemplateDraft`` for transport.

    Used as both:
    - Request body for ``POST /arc-templates`` (final save)
    - Response body for ``POST /generate-full-draft`` (so the wizard
      can pre-fill the review step)
    """

    id: str = Field(..., min_length=1)
    title: str = Field(..., min_length=1)
    premise: str = Field(..., min_length=1)
    theme: str = "custom"
    language: str = ""
    tone: str = "daily"
    duration_days: int = Field(default=14, ge=1, le=365)
    world_frames: list[str] = Field(default_factory=list)
    required_traits: list[str] = Field(default_factory=list)
    applicability_scope: str = ARC_TEMPLATE_SCOPE_GENERIC
    target_character_ids: list[str] = Field(default_factory=list)
    beats: list[BeatDraftPayload] = Field(default_factory=list)

    def to_domain(self) -> TemplateDraft:
        return TemplateDraft(
            id=self.id,
            title=self.title,
            premise=self.premise,
            theme=self.theme,
            language=self.language,
            tone=self.tone,
            duration_days=self.duration_days,
            world_frames=tuple(self.world_frames),
            required_traits=tuple(self.required_traits),
            applicability_scope=self.applicability_scope,
            target_character_ids=tuple(self.target_character_ids),
            beats=tuple(b.to_domain() for b in self.beats),
        )

    @classmethod
    def from_domain(cls, draft: TemplateDraft) -> "TemplateDraftPayload":
        return cls(
            id=draft.id,
            title=draft.title,
            premise=draft.premise,
            theme=draft.theme,
            language=draft.language,
            tone=draft.tone,
            duration_days=draft.duration_days,
            world_frames=list(draft.world_frames),
            required_traits=list(draft.required_traits),
            applicability_scope=draft.applicability_scope,
            target_character_ids=list(draft.target_character_ids),
            beats=[
                BeatDraftPayload(
                    sequence=b.sequence,
                    day_offset=b.day_offset,
                    title=b.title,
                    summary=b.summary,
                    tension=b.tension,
                    scene_type=b.scene_type,
                    location=b.location,
                    scene_characters=list(b.scene_characters),
                    dramatic_question=b.dramatic_question,
                    required=b.required,
                )
                for b in draft.beats
            ],
        )


@router.post(
    "/arc-templates/intake/generate-full-draft",
    response_model=TemplateDraftPayload | None,
    summary="一鍵：從 pitch 直接生整份 template 草稿（操作者再 review）",
)
async def generate_full_draft(
    payload: GenerateFullDraftRequest,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> TemplateDraftPayload | None:
    service = _require_intake_service(container)
    language = await resolve_stored_operator_primary_language(
        container, current_user_id,
    )
    draft = await service.generate_full_draft(
        pitch=payload.pitch, hint=payload.hint,
        operator_primary_language=language,
    )
    if draft is None:
        return None
    return TemplateDraftPayload.from_domain(draft)


# ---------- Save -------------------------------------------------------


class SaveTemplateRequest(BaseModel):
    draft: TemplateDraftPayload
    overwrite: bool = False


class SaveTemplateResponse(BaseModel):
    template_id: str
    template: ArcTemplateResponse


@router.post(
    "/arc-templates",
    response_model=SaveTemplateResponse,
    status_code=status.HTTP_201_CREATED,
    summary="收尾：把整份 draft 存進 arc_templates 表（owner = current user）",
)
async def save_template(
    payload: SaveTemplateRequest,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> SaveTemplateResponse:
    service = _require_intake_service(container)
    await _assert_target_characters_visible(
        container,
        character_ids=payload.draft.target_character_ids,
        current_user_id=current_user_id,
    )
    language = await resolve_stored_operator_primary_language(
        container, current_user_id,
    )
    try:
        template_id = await service.save_template(
            payload.draft.to_domain(),
            user_id=current_user_id,
            overwrite=payload.overwrite,
            operator_language=language,
        )
    except ValueError as exc:
        # ``id collision without overwrite`` / pack collision / empty
        # beats all surface as ValueError. 409 is the right shape so
        # the wizard can show "id 已存在，要覆寫嗎？" (or refuse for
        # pack collisions, which are non-recoverable from the UI).
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc

    # Round-trip: re-load through the same ownership filter the rest of
    # the surface uses so the response can't accidentally include a row
    # this user wouldn't otherwise see.
    template = None
    if container.arc_template_repository is not None:
        template = await container.arc_template_repository.get_for_user(
            template_id, user_id=current_user_id,
        )
    if template is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Template saved but could not be re-loaded",
        )
    return SaveTemplateResponse(
        template_id=template_id,
        template=ArcTemplateResponse.from_domain(template),
    )


# ---------- Scaffolds catalogue ----------------------------------------


# NOTE: player-visible labels/descriptions are NOT stored here (plan #4 /
# D6). Each entry carries a stable ``id`` + language-neutral structural
# fields only; the frontend maps ``id`` → trilingual label/description via
# ``story.arcTemplateIntake.scaffolds.*``. Never add zh-TW display strings
# back into this catalogue.
_RHYTHM_SCAFFOLDS: list[dict] = [
    {
        "id": "classic_three_act",
        "recommended_duration": [10, 30],
        "recommended_beat_count": [6, 8],
        "default_distribution_14d": [
            {"day_offset": 0, "tension": "setup", "scene_type": "encounter"},
            {"day_offset": 2, "tension": "rising", "scene_type": "encounter"},
            {"day_offset": 5, "tension": "rising", "scene_type": "conflict"},
            {"day_offset": 8, "tension": "rising", "scene_type": "revelation"},
            {"day_offset": 11, "tension": "climax", "scene_type": "conflict"},
            {"day_offset": 13, "tension": "falling", "scene_type": "interlude"},
            {"day_offset": 14, "tension": "resolution", "scene_type": "resolution"},
        ],
    },
    {
        "id": "gradual_awakening",
        "recommended_duration": [10, 21],
        "recommended_beat_count": [5, 7],
        "default_distribution_14d": [
            {"day_offset": 0, "tension": "setup", "scene_type": "encounter"},
            {"day_offset": 3, "tension": "rising", "scene_type": "revelation"},
            {"day_offset": 6, "tension": "rising", "scene_type": "interlude"},
            {"day_offset": 9, "tension": "rising", "scene_type": "revelation"},
            {"day_offset": 12, "tension": "climax", "scene_type": "revelation"},
            {"day_offset": 14, "tension": "resolution", "scene_type": "resolution"},
        ],
    },
    {
        "id": "quiet_ending",
        "recommended_duration": [7, 14],
        "recommended_beat_count": [5, 7],
        "default_distribution_14d": [
            {"day_offset": 0, "tension": "setup", "scene_type": "interlude"},
            {"day_offset": 2, "tension": "rising", "scene_type": "interlude"},
            {"day_offset": 5, "tension": "rising", "scene_type": "revelation"},
            {"day_offset": 8, "tension": "rising", "scene_type": "revelation"},
            {"day_offset": 11, "tension": "climax", "scene_type": "conflict"},
            {"day_offset": 13, "tension": "falling", "scene_type": "resolution"},
            {"day_offset": 14, "tension": "resolution", "scene_type": "resolution"},
        ],
    },
    {
        "id": "big_decision",
        "recommended_duration": [5, 10],
        "recommended_beat_count": [4, 5],
        "default_distribution_14d": [
            {"day_offset": 0, "tension": "setup", "scene_type": "encounter"},
            {"day_offset": 4, "tension": "rising", "scene_type": "conflict"},
            {"day_offset": 8, "tension": "rising", "scene_type": "conflict"},
            {"day_offset": 12, "tension": "climax", "scene_type": "conflict"},
            {"day_offset": 14, "tension": "resolution", "scene_type": "resolution"},
        ],
    },
    {
        "id": "slow_burn",
        "recommended_duration": [21, 30],
        "recommended_beat_count": [7, 10],
        "default_distribution_14d": [
            {"day_offset": 0, "tension": "setup", "scene_type": "encounter"},
            {"day_offset": 3, "tension": "setup", "scene_type": "interlude"},
            {"day_offset": 5, "tension": "rising", "scene_type": "encounter"},
            {"day_offset": 8, "tension": "rising", "scene_type": "revelation"},
            {"day_offset": 10, "tension": "rising", "scene_type": "conflict"},
            {"day_offset": 12, "tension": "climax", "scene_type": "conflict"},
            {"day_offset": 13, "tension": "falling", "scene_type": "resolution"},
            {"day_offset": 14, "tension": "resolution", "scene_type": "resolution"},
        ],
    },
]


# Stable ``id`` enums only — labels/descriptions live in the frontend
# trilingual bundle (plan #4 / D6). Do not reintroduce zh-TW strings.
_TONE_CATALOGUE: list[dict] = [
    {"id": "daily"},
    {"id": "dramatic"},
    {"id": "mature"},
    {"id": "dark"},
    {"id": "lighthearted"},
]


_THEME_CATALOGUE: list[dict] = [
    {"id": "ambition"},
    {"id": "friendship"},
    {"id": "loss"},
    {"id": "discovery"},
    {"id": "transformation"},
    {"id": "redemption"},
    {"id": "custom"},
]


_SCENE_TYPE_CATALOGUE: list[dict] = [
    {"id": "encounter"},
    {"id": "revelation"},
    {"id": "conflict"},
    {"id": "resolution"},
    {"id": "interlude"},
]


class ScaffoldsResponse(BaseModel):
    rhythm_patterns: list[dict]
    tones: list[dict]
    themes: list[dict]
    scene_types: list[dict]
    world_frames: list[str]


@router.get(
    "/arc-templates/scaffolds",
    response_model=ScaffoldsResponse,
    summary="Wizard 用：列出所有節奏 pattern / tone / theme / scene_type / world_frame 選項",
)
async def get_scaffolds() -> ScaffoldsResponse:
    return ScaffoldsResponse(
        rhythm_patterns=_RHYTHM_SCAFFOLDS,
        tones=_TONE_CATALOGUE,
        themes=_THEME_CATALOGUE,
        scene_types=_SCENE_TYPE_CATALOGUE,
        world_frames=["modern", "fantasy", "school", "custom"],
    )
