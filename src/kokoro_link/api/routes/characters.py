import logging
from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks, Body, Depends, File, Form, HTTPException, Query, Response, UploadFile, status
from pydantic import BaseModel, Field, ValidationError

from kokoro_link.api.dependencies import (
    ensure_owned_character_id,
    get_container,
    get_current_user,
    get_current_user_id,
    get_owned_character,
    require_admin,
)
from kokoro_link.api.character_runtime import (
    ensure_character_primary_image,
    enqueue_character_runtime_initialization,
)
from kokoro_link.api.operator_language import (
    resolve_operator_primary_language,
    resolve_stored_operator_primary_language,
)
from kokoro_link.application.dto.character import (
    CharacterResponse,
    CommitCandidatesRequest,
    CreateCharacterRequest,
    FeatureImageProfileOverridePayload,
    FeatureModelOverridePayload,
    FeatureVideoProfileOverridePayload,
    GenerateCandidatesRequest,
    GenerateCandidatesResponse,
    GeneratePortraitRequest,
    ProactiveRhythmRequest,
    ResetCharacterDataRequest,
    ResetCharacterDataResponse,
    StateSnapshotResponse,
    UpdateCharacterRequest,
    proactive_rhythm_values,
    InitialRelationshipPayload,
    InitialRelationshipSafeUserProfilePayload,
)
from kokoro_link.domain.entities.operator_profile import OperatorProfile
from kokoro_link.application.dto.scene_access import StageAccessVerdictResponse
from kokoro_link.application.services.scene_access_service import (
    CharacterNotFoundError as SceneAccessCharacterNotFoundError,
)
from kokoro_link.application.services.feature_keys import (
    CHARACTER_FEATURE_KEYS,
    FEATURE_LABELS,
    IMAGE_FEATURE_KEYS,
    VIDEO_FEATURE_KEYS,
)
from kokoro_link.application.dto.character_draft import (
    CharacterDraftResponse,
    GenerateCompanionsRequest,
    GenerateCompanionsResponse,
)
from kokoro_link.application.services.companion_draft_service import (
    CharacterNotFoundError as CompanionGenCharacterNotFoundError,
)
from kokoro_link.application.services.character_image_service import (
    CharacterImageError,
    GenerationDisabledError,
    GenerationFailedError,
    ImageNotFoundError,
    ImageTooLargeError,
    MAX_IMAGE_BYTES,
    TooManyImagesError,
    UnsupportedImageTypeError,
)
from kokoro_link.application.services.character_card_export_service import (
    CharacterCardNotFoundError,
)
from kokoro_link.application.dto.character_card import CharacterCardPreview
from kokoro_link.application.services.character_card_import_service import (
    CharacterCardImportError,
    InvalidCharacterCardError,
    UnsupportedCardSchemaError,
)
from kokoro_link.application.services.sillytavern_convert_service import (
    ConvertedSillyTavernCard,
    SillyTavernConvertService,
)
from kokoro_link.infrastructure.character_card.packager import (
    pack_character_card,
)
from kokoro_link.infrastructure.character_card.sillytavern import (
    CardKind,
    InvalidSillyTavernCardError,
    UnsupportedSillyTavernCardError,
    extract_png_chara_chunk,
    parse_sillytavern_json,
    sniff_card_kind,
)
from kokoro_link.application.services.character_service import (
    CharacterValidationError,
)
from kokoro_link.application.services.character_creation_intake_service import (
    CharacterCreationDraftContext,
    CharacterCreationIntakeService,
)
from kokoro_link.application.services.character_lora_service import (
    CharacterLoraError,
    LoraNotFoundError,
    LoraTooLargeError,
    LoraUploadDisabledError,
    UnsupportedLoraTypeError,
)
from kokoro_link.bootstrap.container import ServiceContainer
from kokoro_link.contracts.character_draft import ImageInput
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.value_objects.presence_frame import ChatSurface

router = APIRouter(tags=["characters"])

_LOGGER = logging.getLogger(__name__)

_MAX_IMAGE_BYTES = MAX_IMAGE_BYTES


class CharacterCreationDraftPayload(BaseModel):
    name: str = ""
    summary: str = ""
    personality: list[str] = Field(default_factory=list)
    interests: list[str] = Field(default_factory=list)
    speaking_style: str = ""
    boundaries: list[str] = Field(default_factory=list)
    aspirations: list[str] = Field(default_factory=list)
    personality_type_code: str = ""
    personality_type_rationale: str = ""

    def to_domain(self) -> CharacterCreationDraftContext:
        return CharacterCreationDraftContext(
            name=self.name,
            summary=self.summary,
            personality=tuple(self.personality),
            interests=tuple(self.interests),
            speaking_style=self.speaking_style,
            boundaries=tuple(self.boundaries),
            aspirations=tuple(self.aspirations),
            personality_type_code=self.personality_type_code,
            personality_type_rationale=self.personality_type_rationale,
        )


# Highest follow-up round the intake analysis escalates through. The frontend
# clamps to the same value; the handler clamps again so an over-counting or
# out-of-date client cannot trip request validation. Mirror of
# ``MAX_CREATION_INTAKE_ROUND`` in ``frontend/src/utils/characterCreationIntake.ts``.
_MAX_INTAKE_ROUND = 2


class CharacterCreationIntakeAnalyzeRequest(BaseModel):
    character_draft: CharacterCreationDraftPayload = Field(
        default_factory=CharacterCreationDraftPayload,
    )
    relationship: InitialRelationshipPayload = Field(
        default_factory=InitialRelationshipPayload,
    )
    current_locale: str = ""
    # Only reject negatives here. The upper bound is *clamped* in the handler
    # (not rejected) so a client that over-counts follow-up rounds never turns
    # a 422 into "the already-shown suggestions vanished". See _MAX_INTAKE_ROUND.
    round_index: int = Field(default=0, ge=0)


class CharacterCreationIntakeQuestionResponse(BaseModel):
    field: str
    question: str
    suggestions: list[str] = Field(default_factory=list)


class CharacterCreationIntakeWarningResponse(BaseModel):
    kind: str
    message: str
    blocking: bool = False


class CharacterCreationIntakeAnalyzeResponse(BaseModel):
    can_create: bool
    missing_required: list[str] = Field(default_factory=list)
    questions: list[CharacterCreationIntakeQuestionResponse] = Field(
        default_factory=list,
    )
    normalized_relationship: InitialRelationshipPayload = Field(
        default_factory=InitialRelationshipPayload,
    )
    normalized_user_profile: InitialRelationshipSafeUserProfilePayload = Field(
        default_factory=InitialRelationshipSafeUserProfilePayload,
    )
    warnings: list[CharacterCreationIntakeWarningResponse] = Field(
        default_factory=list,
    )


def _require_character_creation_intake_service(
    container: ServiceContainer,
) -> CharacterCreationIntakeService:
    service = getattr(container, "character_creation_intake_service", None)
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Character creation intake service not configured",
        )
    return service


@router.get("/characters", response_model=list[CharacterResponse])
async def list_characters(
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> list[CharacterResponse]:
    return await container.character_service.list_characters(
        user_id=current_user_id,
    )


_ROUTING_OVERRIDE_FIELDS = (
    "feature_models",
    "feature_image_profiles",
    "feature_video_profiles",
)


def _reject_non_admin_routing_overrides(
    user: OperatorProfile,
    payload: CreateCharacterRequest | UpdateCharacterRequest,
) -> None:
    """Per-character ROUTING is admin-only (post-immersion decision).

    The dedicated preference PUTs are admin-gated, but the general
    create/update payloads carry the same override fields — an ungated
    write here would bypass that gate. Rejects with an explicit 403
    naming the field (silently stripping would hide the denial from API
    callers). Rules:

    - Admins pass untouched.
    - ``None`` (PATCH: field omitted or explicit null = "leave alone")
      passes.
    - An empty list passes on CREATE (it's the pydantic default — "no
      overrides") but is rejected on PATCH, where ``[]`` means "clear
      existing pins" — un-pinning an admin-configured route is also a
      routing mutation.
    - Any non-empty list is rejected for non-admins on both routes.
    """
    if user.is_admin:
        return
    empty_list_is_noop = isinstance(payload, CreateCharacterRequest)
    for field in _ROUTING_OVERRIDE_FIELDS:
        value = getattr(payload, field)
        if value is None:
            continue
        if not value and empty_list_is_noop:
            continue
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "admin privilege required to modify per-character "
                f"routing ({field})"
            ),
        )


@router.post("/characters", response_model=CharacterResponse, status_code=status.HTTP_201_CREATED)
async def create_character(
    payload: CreateCharacterRequest,
    background_tasks: BackgroundTasks,
    container: ServiceContainer = Depends(get_container),
    current_user: OperatorProfile = Depends(get_current_user),
    current_user_id: str = Depends(get_current_user_id),
) -> CharacterResponse:
    _reject_non_admin_routing_overrides(current_user, payload)
    try:
        character = await container.character_service.create_character(
            payload, user_id=current_user_id,
        )
    except CharacterValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    character = await ensure_character_primary_image(
        container=container,
        character=character,
        user_id=current_user_id,
    )
    enqueue_character_runtime_initialization(
        background_tasks,
        container=container,
        character=character,
        user_id=current_user_id,
    )
    return character


@router.post("/characters/draft", response_model=CharacterDraftResponse)
async def draft_character(
    prompt: str | None = Form(default=None),
    image: UploadFile | None = File(default=None),
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> CharacterDraftResponse:
    if (prompt is None or not prompt.strip()) and image is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Provide a prompt, an image, or both",
        )

    image_input: ImageInput | None = None
    if image is not None:
        data = await image.read()
        if len(data) > _MAX_IMAGE_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="Image exceeds 8 MB limit",
            )
        mime = image.content_type or "image/png"
        image_input = ImageInput(data=data, mime_type=mime)

    return await container.character_draft_service.generate(
        prompt=prompt,
        image=image_input,
        operator_primary_language=await resolve_operator_primary_language(
            container,
            current_user_id,
        ),
        operator_id=current_user_id,
    )


@router.post(
    "/characters/creation-intake/analyze",
    response_model=CharacterCreationIntakeAnalyzeResponse,
)
async def analyze_character_creation_intake(
    payload: CharacterCreationIntakeAnalyzeRequest,
    container: ServiceContainer = Depends(get_container),
) -> CharacterCreationIntakeAnalyzeResponse:
    service = _require_character_creation_intake_service(container)
    result = await service.analyze(
        draft=payload.character_draft.to_domain(),
        relationship=payload.relationship,
        current_locale=payload.current_locale,
        round_index=min(payload.round_index, _MAX_INTAKE_ROUND),
    )
    return CharacterCreationIntakeAnalyzeResponse(
        can_create=result.can_create,
        missing_required=list(result.missing_required),
        questions=[
            CharacterCreationIntakeQuestionResponse(
                field=item.field,
                question=item.question,
                suggestions=list(item.suggestions),
            )
            for item in result.questions
        ],
        normalized_relationship=result.normalized_relationship,
        normalized_user_profile=result.normalized_user_profile,
        warnings=[
            CharacterCreationIntakeWarningResponse(
                kind=item.kind,
                message=item.message,
                blocking=item.blocking,
            )
            for item in result.warnings
        ],
    )


class ImportCharacterCardResponse(BaseModel):
    """Result of importing a ``.lumecard``: the brand-new character plus
    the ids of any arc templates that were landed alongside it (after
    collision remap)."""

    character: CharacterResponse
    landed_arc_template_ids: list[str] = Field(default_factory=list)
    landed_arc_series_ids: list[str] = Field(default_factory=list)


# A .lumecard is small (a manifest + a handful of stage images + a few
# YAML templates). The packager already caps the *uncompressed* payload;
# this guards the *upload* so a multi-GB body can't be streamed into
# memory before we even unpack it.
_MAX_CARD_BYTES = 64 * 1024 * 1024


def _parse_initial_relationship_form(
    payload: str | None,
) -> InitialRelationshipPayload | None:
    if payload is None or not payload.strip():
        return None
    try:
        return InitialRelationshipPayload.model_validate_json(payload)
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc


async def _maybe_convert_sillytavern_card(
    data: bytes,
    *,
    container: ServiceContainer,
    user_id: str,
) -> tuple[bytes, ConvertedSillyTavernCard | None]:
    """Sniff an upload and, when it is a SillyTavern card, convert it into
    an in-memory ``.lumecard`` blob the existing import/preview service can
    consume unchanged (D1/D3 route-layer sniffing).

    Returns ``(blob, converted)`` — ``converted`` is ``None`` for the
    native ``.lumecard`` path (blob returned untouched) and carries the
    ST-only extras (suggested context / dropped fields) otherwise.

    Raises the same HTTP errors the ``.lumecard`` path maps: an
    unsupported ST spec → 422, a malformed ST card → 400.
    """
    kind = sniff_card_kind(data)
    if kind is CardKind.LUMECARD:
        return data, None
    if kind is CardKind.UNKNOWN:
        # A real .lumecard always starts with PK; anything unrecognised is
        # a bad upload. Mirror the packager's 400 for a non-zip blob.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="unrecognised character card format",
        )

    convert_service: SillyTavernConvertService | None = (
        container.sillytavern_convert_service
    )
    if convert_service is None:  # pragma: no cover — wired in build_container
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="SillyTavern card import is not available",
        )

    png: bytes | None = None
    try:
        if kind is CardKind.SILLYTAVERN_PNG:
            png = data
            card_text = extract_png_chara_chunk(data)
        else:
            card_text = data.decode("utf-8", errors="strict")
        card = parse_sillytavern_json(card_text)
    except UnsupportedSillyTavernCardError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc),
        ) from exc
    except (InvalidSillyTavernCardError, UnicodeDecodeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc),
        ) from exc

    operator_language = await resolve_stored_operator_primary_language(
        container, user_id,
    )
    converted = await convert_service.to_manifest(
        card,
        png=png,
        operator_primary_language=operator_language,
        operator_id=user_id,
    )
    stage_images = (
        [(converted.manifest.stage_images[0], png)]
        if png and converted.manifest.stage_images
        else []
    )
    blob = pack_character_card(
        manifest_json=converted.manifest.model_dump_json(indent=2),
        stage_images=stage_images,
        arc_templates=[],
    )
    return blob, converted


@router.post("/characters/import", response_model=ImportCharacterCardResponse)
async def import_character_card(
    background_tasks: BackgroundTasks,
    card: UploadFile = File(...),
    translate: bool = Form(default=False),
    initial_relationship: str | None = Form(default=None),
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> ImportCharacterCardResponse:
    """Create a brand-new character from an uploaded ``.lumecard``.

    Only the portable A-layer settings (+ bundled arc templates and stage
    images) cross over; B-layer routing and all C-layer runtime data are
    intentionally absent. The multipart request may include an
    importer-confirmed ``initial_relationship`` JSON payload for the new
    local character/operator pair. See ``docs/CHARACTER_CARD_PLAN.md``."""
    service = container.character_card_import_service
    if service is None:  # pragma: no cover — wired in build_container
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Character card import is not available",
        )
    data = await card.read()
    if len(data) > _MAX_CARD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Character card exceeds the 64 MB upload limit",
        )
    data, _converted = await _maybe_convert_sillytavern_card(
        data, container=container, user_id=current_user_id,
    )
    initial_relationship_payload = _parse_initial_relationship_form(
        initial_relationship,
    )
    try:
        result = await service.import_card(
            data,
            user_id=current_user_id,
            translate=translate,
            target_language=(
                await resolve_stored_operator_primary_language(
                    container,
                    current_user_id,
                )
                if translate
                else ""
            ),
            initial_relationship=initial_relationship_payload,
        )
    except UnsupportedCardSchemaError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc),
        ) from exc
    except (InvalidCharacterCardError, CharacterCardImportError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc),
        ) from exc
    enqueue_character_runtime_initialization(
        background_tasks,
        container=container,
        character=result.character,
        user_id=current_user_id,
    )
    character = await ensure_character_primary_image(
        container=container,
        character=result.character,
        user_id=current_user_id,
    )
    return ImportCharacterCardResponse(
        character=character,
        landed_arc_template_ids=result.landed_arc_template_ids,
        landed_arc_series_ids=result.landed_arc_series_ids,
    )


@router.post("/characters/card/preview", response_model=CharacterCardPreview)
async def preview_character_card(
    card: UploadFile = File(...),
    translate: bool = Form(default=False),
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> CharacterCardPreview:
    """Validate and preview a ``.lumecard`` without creating a character."""
    service = container.character_card_import_service
    if service is None:  # pragma: no cover — wired in build_container
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Character card import is not available",
        )
    data = await card.read()
    if len(data) > _MAX_CARD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Character card exceeds the 64 MB upload limit",
        )
    data, converted = await _maybe_convert_sillytavern_card(
        data, container=container, user_id=current_user_id,
    )
    try:
        preview = await service.preview_card(
            data,
            translate=translate,
            target_language=(
                await resolve_stored_operator_primary_language(
                    container,
                    current_user_id,
                )
                if translate
                else ""
            ),
        )
    except UnsupportedCardSchemaError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc),
        ) from exc
    except (InvalidCharacterCardError, CharacterCardImportError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc),
        ) from exc
    if converted is not None:
        # Fold the ST-only hints onto the shared preview shape so the
        # frontend can show the "AI-normalised" notice, the dropped-field
        # note (D7), and pre-fill the relationship wizard (D5). These stay
        # empty on the native ``.lumecard`` path.
        return preview.model_copy(
            update={
                "source_format": "sillytavern",
                "dropped_fields": list(converted.dropped_fields),
                "suggested_known_context": converted.suggested_known_context,
            },
        )
    return preview


@router.post(
    "/characters/{character_id}/companions/generate",
    response_model=GenerateCompanionsResponse,
)
async def generate_companions(
    character_id: str,
    payload: GenerateCompanionsRequest,
    container: ServiceContainer = Depends(get_container),
    _owned_character_id: str = Depends(ensure_owned_character_id),
    current_user_id: str = Depends(get_current_user_id),
) -> GenerateCompanionsResponse:
    """Ask the LLM for a few private NPC sketches for this character.

    Suggestions come back with ``id=null``; the operator's UI is
    expected to let them accept / edit / discard before persisting via
    the regular ``PATCH /characters/{id}`` with the merged companion
    list. Hint is optional ("再多生兩個她在公司的同事")."""
    try:
        suggestions = (
            await container.companion_draft_service.generate_for_character(
                character_id,
                hint=payload.hint,
                count=payload.count,
                operator_primary_language=await resolve_operator_primary_language(
                    container,
                    current_user_id,
                ),
            )
        )
    except CompanionGenCharacterNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Character not found",
        ) from exc
    return GenerateCompanionsResponse(suggestions=suggestions)


@router.get("/characters/{character_id}", response_model=CharacterResponse)
async def get_character(
    character_id: str,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> CharacterResponse:
    character = await container.character_service.get_character(
        character_id, user_id=current_user_id,
    )
    if character is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Character not found")
    return character


@router.get(
    "/characters/{character_id}/stage-access",
    response_model=StageAccessVerdictResponse,
)
async def get_stage_access(
    character_id: str,
    surface: ChatSurface = Query(default=ChatSurface.WEB_STAGE),
    container: ServiceContainer = Depends(get_container),
    _owned_character_id: str = Depends(ensure_owned_character_id),
    current_user_id: str = Depends(get_current_user_id),
) -> StageAccessVerdictResponse:
    service = getattr(container, "scene_access_service", None)
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Scene access service is not available",
        )
    try:
        verdict = await service.evaluate(
            character_id,
            operator_id=current_user_id,
            requested_surface=surface,
            current_user_id=current_user_id,
        )
    except SceneAccessCharacterNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Character not found",
        ) from exc
    return StageAccessVerdictResponse.from_domain(verdict)


@router.patch("/characters/{character_id}", response_model=CharacterResponse)
async def update_character(
    character_id: str,
    payload: UpdateCharacterRequest,
    container: ServiceContainer = Depends(get_container),
    current_user: OperatorProfile = Depends(get_current_user),
    current_user_id: str = Depends(get_current_user_id),
) -> CharacterResponse:
    _reject_non_admin_routing_overrides(current_user, payload)
    try:
        character = await container.character_service.update_character(
            character_id, payload, user_id=current_user_id,
        )
    except CharacterValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    if character is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Character not found")
    return character


@router.patch(
    "/characters/{character_id}/proactive-rhythm",
    response_model=CharacterResponse,
)
async def update_character_proactive_rhythm(
    character_id: str,
    payload: ProactiveRhythmRequest,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> CharacterResponse:
    daily_limit, cooldown_minutes = proactive_rhythm_values(payload.rhythm)
    character = await container.character_service.update_character(
        character_id,
        UpdateCharacterRequest(
            proactive_daily_limit=daily_limit,
            proactive_cooldown_minutes=cooldown_minutes,
        ),
        user_id=current_user_id,
    )
    if character is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Character not found",
        )
    return character


@router.delete("/characters/{character_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_character(
    character_id: str,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> Response:
    removed = await container.character_service.delete_character(
        character_id, user_id=current_user_id,
    )
    if not removed:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Character not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/characters/{character_id}/reset",
    response_model=ResetCharacterDataResponse,
)
async def reset_character_data(
    character_id: str,
    payload: ResetCharacterDataRequest,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> ResetCharacterDataResponse:
    result = await container.character_service.reset_character_data(
        character_id,
        memories=payload.memories,
        conversations=payload.conversations,
        state_history=payload.state_history,
        operator_persona=payload.operator_persona,
        user_id=current_user_id,
    )
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Character not found")
    (
        memories_deleted,
        conversations_deleted,
        state_history_deleted,
        operator_persona_deleted,
    ) = result
    return ResetCharacterDataResponse(
        character_id=character_id,
        memories_deleted=memories_deleted,
        conversations_deleted=conversations_deleted,
        state_history_deleted=state_history_deleted,
        operator_persona_deleted=operator_persona_deleted,
    )


@router.get("/characters/{character_id}/card")
async def export_character_card(
    character_id: str,
    include_arc_template_ids: list[str] | None = Query(
        default=None,
        description="Extra arc-template ids to bundle beyond the bound one",
    ),
    include_arc_series_ids: list[str] | None = Query(
        default=None,
        description="Extra arc-series ids to bundle beyond the bound one",
    ),
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> Response:
    """Export the character's portable A-layer settings (+ bundled arc
    templates and stage images) as a downloadable ``.lumecard`` blob.

    B-layer routing (voice / loras / profiles) and all C-layer runtime
    accumulation are intentionally left out — see
    ``docs/CHARACTER_CARD_PLAN.md``. Ownership is enforced inside the
    service (cross-user → 404, collapsed to avoid enumeration)."""
    service = container.character_card_export_service
    if service is None:  # pragma: no cover — wired in build_container
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Character card export is not available",
        )
    try:
        exported = await service.export(
            character_id,
            user_id=current_user_id,
            include_arc_template_ids=include_arc_template_ids,
            include_arc_series_ids=include_arc_series_ids,
        )
    except CharacterCardNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Character not found",
        ) from exc

    # The slug can be CJK, which isn't valid in a bare ``filename=`` token,
    # so we ship both: an ASCII-safe fallback and the RFC 5987
    # ``filename*`` carrying the real UTF-8 name for modern browsers.
    ascii_fallback = (
        exported.filename.encode("ascii", "ignore").decode() or "character.lumecard"
    )
    disposition = (
        f'attachment; filename="{ascii_fallback}"; '
        f"filename*=UTF-8''{quote(exported.filename)}"
    )
    return Response(
        content=exported.blob,
        media_type="application/octet-stream",
        headers={"Content-Disposition": disposition},
    )


@router.post(
    "/characters/{character_id}/images",
    response_model=CharacterResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upload_character_image(
    character_id: str,
    image: UploadFile = File(...),
    container: ServiceContainer = Depends(get_container),
    _owned_character_id: str = Depends(ensure_owned_character_id),
) -> CharacterResponse:
    data = await image.read()
    try:
        updated = await container.character_image_service.add_image(
            character_id,
            data=data,
            mime_type=image.content_type,
            original_filename=image.filename,
        )
    except ImageTooLargeError as exc:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=str(exc),
        ) from exc
    except UnsupportedImageTypeError as exc:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=str(exc),
        ) from exc
    except TooManyImagesError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    except ImageNotFoundError as exc:
        # Raised when the character doesn't exist.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except CharacterImageError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    return CharacterResponse.from_domain(updated)


@router.post(
    "/characters/{character_id}/images/generate",
    response_model=CharacterResponse,
    status_code=status.HTTP_201_CREATED,
)
async def generate_character_portrait(
    character_id: str,
    payload: GeneratePortraitRequest,
    container: ServiceContainer = Depends(get_container),
    _owned_character_id: str = Depends(ensure_owned_character_id),
) -> CharacterResponse:
    """Render a portrait via ComfyUI and append it to the character's
    permanent image library (same store as manual uploads)."""
    try:
        updated = await container.character_image_service.generate_portrait(
            character_id,
            positive=payload.positive,
            aspect=payload.aspect,
        )
    except GenerationDisabledError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    except TooManyImagesError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    except ImageNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except GenerationFailedError as exc:
        # 502 detail surfaces the message, but the traceback is otherwise
        # lost. Log it explicitly so the next failure leaves a breadcrumb
        # — without this, the access log shows a bare "502 Bad Gateway"
        # with no upstream context.
        _LOGGER.exception(
            "image generate_portrait failed (character=%s, aspect=%s)",
            character_id, payload.aspect,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc
    except CharacterImageError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    return CharacterResponse.from_domain(updated)


@router.post(
    "/characters/{character_id}/images/candidates",
    response_model=GenerateCandidatesResponse,
    status_code=status.HTTP_201_CREATED,
)
async def generate_candidate_portraits(
    character_id: str,
    payload: GenerateCandidatesRequest,
    container: ServiceContainer = Depends(get_container),
    _owned_character_id: str = Depends(ensure_owned_character_id),
) -> GenerateCandidatesResponse:
    """Render a batch of candidate portraits (gacha flow)."""
    try:
        _, urls = await container.character_image_service.generate_candidates(
            character_id,
            positive=payload.positive,
            aspect=payload.aspect,
            count=payload.count,
        )
    except GenerationDisabledError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc),
        ) from exc
    except TooManyImagesError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc),
        ) from exc
    except ImageNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc),
        ) from exc
    except GenerationFailedError as exc:
        _LOGGER.exception(
            "image generate_candidates failed (character=%s, aspect=%s, count=%s)",
            character_id, payload.aspect, payload.count,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc),
        ) from exc
    except CharacterImageError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc),
        ) from exc
    return GenerateCandidatesResponse(
        character_id=character_id, candidates=urls,
    )


@router.post(
    "/characters/{character_id}/images/candidates/commit",
    response_model=CharacterResponse,
)
async def commit_candidate_portraits(
    character_id: str,
    payload: CommitCandidatesRequest,
    container: ServiceContainer = Depends(get_container),
    _owned_character_id: str = Depends(ensure_owned_character_id),
) -> CharacterResponse:
    """Keep selected candidates (stage + album); delete the rest."""
    try:
        updated, album_entries = (
            await container.character_image_service.commit_candidates(
                character_id,
                keep_urls=payload.keep_urls,
                album_urls=payload.album_urls,
            )
        )
    except ImageNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc),
        ) from exc
    # Register album picks after the file-move succeeded. Failure here
    # leaves the file on disk but no album row — logged and swallowed
    # rather than raised, so a partial album registration doesn't
    # undo the stage commit the operator already sees.
    for entry in album_entries:
        try:
            await container.album_service.add_from_candidate(
                character_id=character_id,
                url=entry.url,
                byte_size=entry.byte_size,
            )
        except Exception:  # noqa: BLE001 — best-effort album registration
            _LOGGER.exception(
                "commit_candidates: album registration failed url=%s", entry.url,
            )
    return CharacterResponse.from_domain(updated)


@router.delete(
    "/characters/{character_id}/images",
    response_model=CharacterResponse,
)
async def delete_character_image(
    character_id: str,
    url: str = Query(..., description="The image URL to remove"),
    container: ServiceContainer = Depends(get_container),
    _owned_character_id: str = Depends(ensure_owned_character_id),
) -> CharacterResponse:
    try:
        updated = await container.character_image_service.remove_image(
            character_id, url=url,
        )
    except ImageNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    return CharacterResponse.from_domain(updated)


@router.put(
    "/characters/{character_id}/images/order",
    response_model=CharacterResponse,
)
async def reorder_character_images(
    character_id: str,
    order: list[str] = Body(..., embed=True),
    container: ServiceContainer = Depends(get_container),
    _owned_character_id: str = Depends(ensure_owned_character_id),
) -> CharacterResponse:
    try:
        updated = await container.character_image_service.reorder_images(
            character_id, url_order=order,
        )
    except ImageNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    return CharacterResponse.from_domain(updated)


@router.get(
    "/characters/{character_id}/loras/available",
    response_model=list[str],
)
async def list_available_loras(
    container: ServiceContainer = Depends(get_container),
    _owned_character_id: str = Depends(ensure_owned_character_id),
) -> list[str]:
    """Scan the configured ``lora_dir`` for ``.safetensors`` filenames.

    Empty list when ``lora_dir`` isn't set — the frontend uses that to
    hide the "attach existing" option and only offer upload.
    """
    return container.character_lora_service.list_available()


@router.post(
    "/characters/{character_id}/loras",
    response_model=CharacterResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upload_character_lora(
    character_id: str,
    lora: UploadFile = File(...),
    strength: float = Form(default=1.0),
    container: ServiceContainer = Depends(get_container),
    _owned_character_id: str = Depends(ensure_owned_character_id),
) -> CharacterResponse:
    data = await lora.read()
    try:
        updated = await container.character_lora_service.upload(
            character_id,
            data=data,
            original_filename=lora.filename or "",
            strength=strength,
        )
    except LoraTooLargeError as exc:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=str(exc),
        ) from exc
    except UnsupportedLoraTypeError as exc:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=str(exc),
        ) from exc
    except LoraUploadDisabledError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    except LoraNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except CharacterLoraError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    return CharacterResponse.from_domain(updated)


@router.post(
    "/characters/{character_id}/loras/attach",
    response_model=CharacterResponse,
)
async def attach_existing_lora(
    character_id: str,
    name: str = Body(..., embed=True),
    strength: float = Body(default=1.0, embed=True),
    container: ServiceContainer = Depends(get_container),
    _owned_character_id: str = Depends(ensure_owned_character_id),
) -> CharacterResponse:
    try:
        updated = await container.character_lora_service.attach_existing(
            character_id, name=name, strength=strength,
        )
    except LoraNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc),
        ) from exc
    except UnsupportedLoraTypeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc),
        ) from exc
    return CharacterResponse.from_domain(updated)


@router.patch(
    "/characters/{character_id}/loras",
    response_model=CharacterResponse,
)
async def update_lora_strength(
    character_id: str,
    name: str = Body(..., embed=True),
    strength: float = Body(..., embed=True),
    container: ServiceContainer = Depends(get_container),
    _owned_character_id: str = Depends(ensure_owned_character_id),
) -> CharacterResponse:
    try:
        updated = await container.character_lora_service.set_strength(
            character_id, name=name, strength=strength,
        )
    except LoraNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc),
        ) from exc
    return CharacterResponse.from_domain(updated)


@router.delete(
    "/characters/{character_id}/loras",
    response_model=CharacterResponse,
)
async def remove_character_lora(
    character_id: str,
    name: str = Query(...),
    container: ServiceContainer = Depends(get_container),
    _owned_character_id: str = Depends(ensure_owned_character_id),
) -> CharacterResponse:
    try:
        updated = await container.character_lora_service.remove(
            character_id, name=name,
        )
    except LoraNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc),
        ) from exc
    return CharacterResponse.from_domain(updated)


class CharacterFeatureModelsResponse(BaseModel):
    """Bundle returned by ``GET /characters/{id}/preferences/feature-models``.

    Mirrors the shape of the global ``feature-models`` endpoint so the
    frontend can reuse its picker component with minimal changes —
    ``overrides`` keyed by feature_key, plus the catalogue + labels the
    UI needs to render the picker rows.
    """

    overrides: dict[str, FeatureModelOverridePayload] = Field(default_factory=dict)
    known_keys: list[str] = Field(default_factory=list)
    labels: dict[str, str] = Field(default_factory=dict)


class CharacterFeatureModelsRequest(BaseModel):
    """Body for ``PUT /characters/{id}/preferences/feature-models``.

    Full replace — sending ``{"overrides": {}}`` clears every per-character
    override. Unknown keys (typos / drift) are silently dropped before
    persisting, same defensive behaviour as the global endpoint."""

    overrides: dict[str, FeatureModelOverridePayload] = Field(default_factory=dict)


def _build_feature_models_response(
    character_response: CharacterResponse,
) -> CharacterFeatureModelsResponse:
    """Project the DTO's flat list into the keyed shape the picker uses."""
    overrides: dict[str, FeatureModelOverridePayload] = {}
    for entry in character_response.feature_models:
        if entry.feature_key in CHARACTER_FEATURE_KEYS:
            overrides[entry.feature_key] = entry
    return CharacterFeatureModelsResponse(
        overrides=overrides,
        known_keys=list(CHARACTER_FEATURE_KEYS),
        labels={k: FEATURE_LABELS.get(k, k) for k in CHARACTER_FEATURE_KEYS},
    )


@router.get(
    "/characters/{character_id}/preferences/feature-models",
    response_model=CharacterFeatureModelsResponse,
)
async def get_character_feature_models(
    character: Character = Depends(get_owned_character),
) -> CharacterFeatureModelsResponse:
    """Return the per-character LLM overrides (and the catalogue of
    feature keys the UI should render pickers for).

    Empty / missing entries mean "inherit from the global feature-models
    pref, then global active-model, then container default"."""
    return _build_feature_models_response(
        CharacterResponse.from_domain(character),
    )


@router.put(
    "/characters/{character_id}/preferences/feature-models",
    response_model=CharacterFeatureModelsResponse,
)
async def set_character_feature_models(
    character_id: str,
    payload: CharacterFeatureModelsRequest,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
    _admin: OperatorProfile = Depends(require_admin),
) -> CharacterFeatureModelsResponse:
    """Persist per-character overrides (full replace).

    Drops unknown keys + all-blank entries before storing, so the row
    stays compact and a typo can't strand a stale pin in the DB. Empty
    ``overrides`` clears every override for this character — same effect
    as PATCHing ``feature_models: []`` on the character endpoint."""
    cleaned: list[FeatureModelOverridePayload] = []
    for key, entry in payload.overrides.items():
        if key not in CHARACTER_FEATURE_KEYS:
            continue
        # Coerce the dict-style key into the entry's canonical
        # ``feature_key`` so a frontend that wrote the wrong nested
        # key gets the dict key wins (matches how the global endpoint
        # treats the same shape).
        normalised = FeatureModelOverridePayload(
            feature_key=key,
            provider_id=entry.provider_id,
            model_id=entry.model_id,
        )
        if normalised.to_domain() is None:
            continue
        cleaned.append(normalised)
    update_payload = UpdateCharacterRequest(feature_models=cleaned)
    updated = await container.character_service.update_character(
        character_id, update_payload, user_id=current_user_id,
    )
    if updated is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Character not found",
        )
    return _build_feature_models_response(updated)


class CharacterImageProfilesResponse(BaseModel):
    """Per-character image profile picks + the catalogue the UI renders."""

    overrides: dict[str, FeatureImageProfileOverridePayload] = Field(
        default_factory=dict,
    )
    known_keys: list[str] = Field(default_factory=list)
    labels: dict[str, str] = Field(default_factory=dict)


class CharacterImageProfilesRequest(BaseModel):
    overrides: dict[str, FeatureImageProfileOverridePayload] = Field(
        default_factory=dict,
    )


def _build_image_profiles_response(
    character_response: CharacterResponse,
) -> CharacterImageProfilesResponse:
    overrides: dict[str, FeatureImageProfileOverridePayload] = {}
    for entry in character_response.feature_image_profiles:
        if entry.feature_key in IMAGE_FEATURE_KEYS:
            overrides[entry.feature_key] = entry
    return CharacterImageProfilesResponse(
        overrides=overrides,
        known_keys=list(IMAGE_FEATURE_KEYS),
        labels={k: FEATURE_LABELS.get(k, k) for k in IMAGE_FEATURE_KEYS},
    )


@router.get(
    "/characters/{character_id}/preferences/image-profiles",
    response_model=CharacterImageProfilesResponse,
)
async def get_character_image_profiles(
    character: Character = Depends(get_owned_character),
) -> CharacterImageProfilesResponse:
    """Per-character image-profile picks. Same fall-through chain as the
    global pref: empty entry = inherit from
    ``image_feature_profiles`` → ``active_image_profile`` → first
    registered profile."""
    return _build_image_profiles_response(CharacterResponse.from_domain(character))


@router.put(
    "/characters/{character_id}/preferences/image-profiles",
    response_model=CharacterImageProfilesResponse,
)
async def set_character_image_profiles(
    character_id: str,
    payload: CharacterImageProfilesRequest,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
    _admin: OperatorProfile = Depends(require_admin),
) -> CharacterImageProfilesResponse:
    """Full-replace persist of the per-character image profile picks.

    Unknown feature keys and unknown profile ids are silently dropped so
    a stale frontend can't poison the row."""
    cleaned: list[FeatureImageProfileOverridePayload] = []
    for key, entry in payload.overrides.items():
        if key not in IMAGE_FEATURE_KEYS:
            continue
        normalised = FeatureImageProfileOverridePayload(
            feature_key=key,
            profile_id=entry.profile_id,
        )
        domain = normalised.to_domain()
        if domain is None:
            continue
        if container.image_profile_registry.get_profile(
            domain.profile_id or "",
        ) is None:
            continue
        cleaned.append(normalised)
    update_payload = UpdateCharacterRequest(feature_image_profiles=cleaned)
    updated = await container.character_service.update_character(
        character_id, update_payload, user_id=current_user_id,
    )
    if updated is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Character not found",
        )
    return _build_image_profiles_response(updated)


class CharacterVideoProfilesResponse(BaseModel):
    overrides: dict[str, FeatureVideoProfileOverridePayload] = Field(
        default_factory=dict,
    )
    known_keys: list[str] = Field(default_factory=list)
    labels: dict[str, str] = Field(default_factory=dict)


class CharacterVideoProfilesRequest(BaseModel):
    overrides: dict[str, FeatureVideoProfileOverridePayload] = Field(
        default_factory=dict,
    )


def _build_video_profiles_response(
    character_response: CharacterResponse,
) -> CharacterVideoProfilesResponse:
    overrides: dict[str, FeatureVideoProfileOverridePayload] = {}
    for entry in character_response.feature_video_profiles:
        if entry.feature_key in VIDEO_FEATURE_KEYS:
            overrides[entry.feature_key] = entry
    return CharacterVideoProfilesResponse(
        overrides=overrides,
        known_keys=list(VIDEO_FEATURE_KEYS),
        labels={k: FEATURE_LABELS.get(k, k) for k in VIDEO_FEATURE_KEYS},
    )


@router.get(
    "/characters/{character_id}/preferences/video-profiles",
    response_model=CharacterVideoProfilesResponse,
)
async def get_character_video_profiles(
    character: Character = Depends(get_owned_character),
) -> CharacterVideoProfilesResponse:
    return _build_video_profiles_response(CharacterResponse.from_domain(character))


@router.put(
    "/characters/{character_id}/preferences/video-profiles",
    response_model=CharacterVideoProfilesResponse,
)
async def set_character_video_profiles(
    character_id: str,
    payload: CharacterVideoProfilesRequest,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
    _admin: OperatorProfile = Depends(require_admin),
) -> CharacterVideoProfilesResponse:
    cleaned: list[FeatureVideoProfileOverridePayload] = []
    for key, entry in payload.overrides.items():
        if key not in VIDEO_FEATURE_KEYS:
            continue
        normalised = FeatureVideoProfileOverridePayload(
            feature_key=key,
            profile_id=entry.profile_id,
        )
        domain = normalised.to_domain()
        if domain is None:
            continue
        if container.video_profile_registry.get_profile(
            domain.profile_id or "",
        ) is None:
            continue
        cleaned.append(normalised)
    update_payload = UpdateCharacterRequest(feature_video_profiles=cleaned)
    updated = await container.character_service.update_character(
        character_id, update_payload, user_id=current_user_id,
    )
    if updated is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Character not found",
        )
    return _build_video_profiles_response(updated)


@router.get(
    "/characters/{character_id}/state-history",
    response_model=list[StateSnapshotResponse],
)
async def get_state_history(
    character_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    container: ServiceContainer = Depends(get_container),
    _owned_character_id: str = Depends(ensure_owned_character_id),
) -> list[StateSnapshotResponse]:
    snapshots = await container.state_history_repository.query(character_id, limit=limit)
    return [StateSnapshotResponse.from_domain(s) for s in snapshots]
