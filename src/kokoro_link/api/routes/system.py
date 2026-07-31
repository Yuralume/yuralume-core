from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from kokoro_link.api.dependencies import (
    get_container,
    get_current_user,
    require_admin,
)
from kokoro_link.application.services.scoped_preferences import (
    delete_user_preference,
    get_preference_with_user_fallback,
    set_user_preference,
)
from kokoro_link.application.services.feature_keys import (
    FEATURE_GROUP_DESCRIPTIONS,
    FEATURE_GROUP_LABELS,
    FEATURE_GROUP_MODEL_GUIDANCE,
    FEATURE_GROUP_MEMBERS,
    FEATURE_LABELS,
    FEATURE_TO_GROUP,
    GLOBAL_FEATURE_KEYS,
    IMAGE_FEATURE_KEYS,
    LLM_FEATURE_GROUP_KEYS,
    VIDEO_FEATURE_KEYS,
)
from kokoro_link.application.services.routing_reasoning import (
    parse_reasoning_override,
    reasoning_override_from_fields,
    reasoning_pref_value,
)
from kokoro_link.application.services.routing_reasoning_validation import (
    ReasoningEffortValidationError,
    RoutingReasoningValidationService,
)
from kokoro_link.application.services.routing_vision import (
    parse_vision_override,
)
from kokoro_link.application.services.tts_pregeneration_service import (
    TTS_PREGENERATION_PREFERENCE_KEY,
)
from kokoro_link.application.services.visual_generation_style import (
    VISUAL_GENERATION_STYLE_DEFAULT,
    VISUAL_GENERATION_STYLE_PREFERENCE_KEY,
    is_supported_visual_generation_style,
    normalise_visual_generation_style,
)
from kokoro_link.bootstrap.container import ServiceContainer
from kokoro_link.domain.entities.operator_profile import OperatorProfile

router = APIRouter(tags=["system"])

_ACTIVE_MODEL_KEY = "active_model"
_FEATURE_MODELS_KEY = "feature_models"
_FEATURE_MODEL_GROUPS_KEY = "feature_model_groups"
_ACTIVE_IMAGE_KEY = "active_image_profile"
_IMAGE_FEATURE_KEY = "image_feature_profiles"
_ACTIVE_VIDEO_KEY = "active_video_profile"
_VIDEO_FEATURE_KEY = "video_feature_profiles"
_TTS_PREGENERATION_KEY = TTS_PREGENERATION_PREFERENCE_KEY
_CHAT_ASSIST_KEY = "chat_assist"
_VISUAL_GENERATION_STYLE_KEY = VISUAL_GENERATION_STYLE_PREFERENCE_KEY


class ActiveModelPreference(BaseModel):
    """Global picker state — remembered across page loads.

    ``provider_id`` and ``model_id`` are both nullable so the API can
    return "nothing saved yet" as ``{"provider_id": null, "model_id":
    null}`` without tripping validation, and the frontend can write
    back ``model_id = null`` to mean "use provider default".

    ``supports_vision`` is a tri-state routing override on the primary
    pick: ``null`` inherits the provider connection's flag, ``true`` /
    ``false`` pin it for calls resolved through active_model (needed
    because an aggregator connection fronts both vision and text-only
    models)."""

    provider_id: str | None = None
    model_id: str | None = None
    supports_vision: bool | None = None


class FeatureReasoningOverride(BaseModel):
    """Optional routing-level reasoning posture on a feature/group entry.

    When present (any field explicitly set), the resolver replaces the
    provider connection's reasoning trio with this one for calls routed
    through the entry — so the same model can run e.g. high effort on
    ``high_reasoning_gates`` and reasoning-off on ``light_observers``.
    An all-default object is normalised away on write (same as absent).
    """

    disable_reasoning: bool = False
    reasoning_effort: str | None = None
    thinking_budget_tokens: int | None = Field(default=None, ge=1)


class FeatureModelEntry(BaseModel):
    """One per-feature override. Same shape as ``ActiveModelPreference``
    plus the optional reasoning posture and the tri-state
    ``supports_vision`` pin, so the frontend can reuse its picker
    component. ``supports_vision`` is ``null`` (inherit connection flag)
    / ``true`` / ``false`` and may be the ONLY thing an entry pins."""

    provider_id: str | None = None
    model_id: str | None = None
    reasoning: FeatureReasoningOverride | None = None
    supports_vision: bool | None = None


class FeatureModelsPreference(BaseModel):
    """Per-feature LLM routing overrides.

    ``overrides`` maps feature key → ``FeatureModelEntry``. Missing /
    null / empty-string fields mean "use whatever the global
    ``active_model`` says" — callers only populate keys they want to
    pin. ``known_keys`` + ``labels`` are echoed for the UI so the
    frontend can render the picker list without importing backend
    constants.
    """

    overrides: dict[str, FeatureModelEntry] = Field(default_factory=dict)
    known_keys: list[str] = Field(default_factory=list)
    labels: dict[str, str] = Field(default_factory=dict)


class FeatureGroupMember(BaseModel):
    key: str
    label: str


class FeatureModelGroupSummary(BaseModel):
    key: str
    label: str
    description: str
    model_guidance: str
    members: list[FeatureGroupMember] = Field(default_factory=list)
    model: FeatureModelEntry | None = None


class FeatureModelGroupsPreference(BaseModel):
    groups: list[FeatureModelGroupSummary] = Field(default_factory=list)
    active_model: ActiveModelPreference = Field(
        default_factory=ActiveModelPreference,
    )


class FeatureModelGroupsUpdate(BaseModel):
    feature_model_groups: dict[str, FeatureModelEntry] = Field(
        default_factory=dict,
    )


class TTSPregenerationPreference(BaseModel):
    enabled: bool = False


class ChatAssistPreference(BaseModel):
    enabled: bool = True


class VisualGenerationStylePreference(BaseModel):
    style: str = VISUAL_GENERATION_STYLE_DEFAULT


class QuietHoursPreference(BaseModel):
    """Operator-local quiet-hours window (inclusive ``start``, exclusive
    ``end``). ``start > end`` represents a window that wraps midnight
    (e.g. 23–07). Both bounds clamped to 0–23 at the service layer."""

    start: int = Field(ge=0, le=23)
    end: int = Field(ge=0, le=23)


@router.get("/system/providers", response_model=list[str])
async def list_providers(
    container: ServiceContainer = Depends(get_container),
) -> list[str]:
    return container.model_registry.list_ids()


@router.get(
    "/system/providers/{provider_id}/models",
    response_model=list[str],
)
async def list_provider_models(
    provider_id: str,
    container: ServiceContainer = Depends(get_container),
) -> list[str]:
    """Return the model IDs the given provider currently offers.

    Lets the frontend populate the second-level "model" dropdown once
    the operator picks a provider. Empty list is a valid answer — the
    UI should render a disabled dropdown in that case."""
    try:
        model = container.model_registry.resolve(provider_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return await model.list_models()


@router.get(
    "/system/preferences/active-model",
    response_model=ActiveModelPreference,
)
async def get_active_model_preference(
    scope: str = Query(default="global"),
    container: ServiceContainer = Depends(get_container),
    current_user: OperatorProfile = Depends(get_current_user),
) -> ActiveModelPreference:
    """Return the persisted provider/model pick, or an empty pair when
    nothing has been saved yet."""
    user_id = _read_preference_user_id(scope, current_user)
    raw = await _get_preference(container, _ACTIVE_MODEL_KEY, user_id=user_id)
    if not isinstance(raw, dict):
        return ActiveModelPreference()
    return ActiveModelPreference(
        provider_id=_coerce_str(raw.get("provider_id")),
        model_id=_coerce_str(raw.get("model_id")),
        supports_vision=parse_vision_override(raw),
    )


@router.put(
    "/system/preferences/active-model",
    response_model=ActiveModelPreference,
)
async def set_active_model_preference(
    payload: ActiveModelPreference,
    scope: str = Query(default="global"),
    container: ServiceContainer = Depends(get_container),
    current_user: OperatorProfile = Depends(require_admin),
) -> ActiveModelPreference:
    """Persist the active provider/model pick. Accepts ``null`` for
    either field — ``model_id = null`` means "use whatever the provider
    considers its default"."""
    user_id = _preference_user_id(scope, current_user)
    value: dict[str, Any] = {
        "provider_id": payload.provider_id,
        "model_id": payload.model_id,
    }
    # Only persist a vision pin when one is set; ``null`` stays absent so
    # the entry inherits the connection flag (and a fully-blank payload
    # still counts as "clear" below).
    if payload.supports_vision is not None:
        value["supports_vision"] = payload.supports_vision
    if (
        user_id
        and payload.provider_id is None
        and payload.model_id is None
        and payload.supports_vision is None
    ):
        await delete_user_preference(
            container.preferences_repository,
            _ACTIVE_MODEL_KEY,
            user_id=user_id,
        )
    else:
        await _set_preference(
            container,
            _ACTIVE_MODEL_KEY,
            value,
            user_id=user_id,
        )
    return payload


def _reasoning_from_raw_entry(entry: Any) -> FeatureReasoningOverride | None:
    """Read a stored entry's reasoning object back into the API shape."""
    overrides = parse_reasoning_override(entry)
    if overrides is None:
        return None
    return FeatureReasoningOverride(
        disable_reasoning=overrides.disable_reasoning,
        reasoning_effort=overrides.reasoning_effort,
        thinking_budget_tokens=overrides.thinking_budget_tokens,
    )


def _cleaned_reasoning_value(
    entry: FeatureModelEntry,
) -> dict[str, object] | None:
    """Normalise a submitted reasoning override to its stored shape.

    All-default objects collapse to ``None`` so a blank reasoning form
    can't keep an otherwise-empty entry alive."""
    if entry.reasoning is None:
        return None
    return reasoning_pref_value(
        reasoning_override_from_fields(
            disable_reasoning=entry.reasoning.disable_reasoning,
            reasoning_effort=entry.reasoning.reasoning_effort,
            thinking_budget_tokens=entry.reasoning.thinking_budget_tokens,
        ),
    )


def _cleaned_routing_entry(entry: FeatureModelEntry) -> dict[str, Any] | None:
    """Shared write-side normalisation for feature and group entries.

    Returns ``None`` when the entry pins nothing at all (provider,
    model, reasoning AND supports_vision blank) — same effect as
    omitting the key. An entry pinning ONLY ``supports_vision`` is real
    configuration and survives."""
    provider_id = _coerce_str(entry.provider_id)
    model_id = _coerce_str(entry.model_id)
    reasoning = _cleaned_reasoning_value(entry)
    # pydantic already validated the field as ``bool | None``; a bool is
    # a pin, ``None`` inherits the connection flag.
    vision = entry.supports_vision if isinstance(entry.supports_vision, bool) else None
    if (
        provider_id is None
        and model_id is None
        and reasoning is None
        and vision is None
    ):
        return None
    value: dict[str, Any] = {
        "provider_id": provider_id,
        "model_id": model_id,
    }
    if reasoning is not None:
        value["reasoning"] = reasoning
    if vision is not None:
        value["supports_vision"] = vision
    return value


def _first_routing_string(
    field: str,
    entries: tuple[dict[str, Any], ...],
) -> str | None:
    """Resolve provider/model independently, matching runtime fall-through."""
    for entry in entries:
        value = _coerce_str(entry.get(field))
        if value is not None:
            return value
    return None


async def _validate_reasoning_effort_entries(
    container: ServiceContainer,
    entries: dict[str, dict[str, Any]],
    *,
    fallbacks: dict[str, tuple[dict[str, Any], ...]],
) -> None:
    """Probe each distinct free-text effort before committing preferences."""
    validator = RoutingReasoningValidationService(container.model_registry)
    validated: set[tuple[str, str | None, str]] = set()
    for key, entry in entries.items():
        overrides = parse_reasoning_override(entry)
        if overrides is None or overrides.reasoning_effort is None:
            continue
        resolution_chain = (entry, *fallbacks.get(key, ()))
        provider_id = _first_routing_string("provider_id", resolution_chain)
        model_id = _first_routing_string("model_id", resolution_chain)
        if provider_id is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=(
                    "reasoning_effort requires an effective provider; "
                    "select an active or route-specific provider first"
                ),
            )
        target = (provider_id, model_id, overrides.reasoning_effort)
        if target in validated:
            continue
        try:
            await validator.validate(
                provider_id=provider_id,
                model_id=model_id,
                effort=overrides.reasoning_effort,
            )
        except ReasoningEffortValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(exc),
            ) from exc
        validated.add(target)


async def _validate_feature_reasoning_efforts(
    container: ServiceContainer,
    entries: dict[str, dict[str, Any]],
    *,
    user_id: str | None,
) -> None:
    raw_groups = await _get_preference(
        container,
        _FEATURE_MODEL_GROUPS_KEY,
        user_id=user_id,
    )
    raw_active = await _get_preference(
        container,
        _ACTIVE_MODEL_KEY,
        user_id=user_id,
    )
    groups = raw_groups if isinstance(raw_groups, dict) else {}
    active = raw_active if isinstance(raw_active, dict) else {}
    fallbacks: dict[str, tuple[dict[str, Any], ...]] = {}
    for feature_key in entries:
        group_entry = groups.get(FEATURE_TO_GROUP.get(feature_key, ""))
        chain = tuple(
            entry for entry in (group_entry, active)
            if isinstance(entry, dict)
        )
        fallbacks[feature_key] = chain
    await _validate_reasoning_effort_entries(
        container,
        entries,
        fallbacks=fallbacks,
    )


async def _validate_group_reasoning_efforts(
    container: ServiceContainer,
    entries: dict[str, dict[str, Any]],
    *,
    user_id: str | None,
) -> None:
    raw_active = await _get_preference(
        container,
        _ACTIVE_MODEL_KEY,
        user_id=user_id,
    )
    active = raw_active if isinstance(raw_active, dict) else {}
    await _validate_reasoning_effort_entries(
        container,
        entries,
        fallbacks={key: (active,) for key in entries},
    )


@router.get(
    "/system/preferences/feature-models",
    response_model=FeatureModelsPreference,
)
async def get_feature_model_preferences(
    scope: str = Query(default="global"),
    container: ServiceContainer = Depends(get_container),
    current_user: OperatorProfile = Depends(get_current_user),
) -> FeatureModelsPreference:
    """Return per-feature provider/model overrides plus the catalogue
    of feature keys the UI should render pickers for.

    Empty / missing overrides mean "inherit from the global
    active-model preference". The frontend writes partial updates back
    via ``PUT`` — only keys it wants to pin need to be sent."""
    user_id = _read_preference_user_id(scope, current_user)
    raw = await _get_preference(container, _FEATURE_MODELS_KEY, user_id=user_id)
    overrides: dict[str, FeatureModelEntry] = {}
    if isinstance(raw, dict):
        for key, entry in raw.items():
            if not isinstance(key, str) or key not in GLOBAL_FEATURE_KEYS:
                # Drop unknown keys — prevents stale entries from old
                # feature names sticking around after a rename.
                continue
            if not isinstance(entry, dict):
                continue
            overrides[key] = FeatureModelEntry(
                provider_id=_coerce_str(entry.get("provider_id")),
                model_id=_coerce_str(entry.get("model_id")),
                reasoning=_reasoning_from_raw_entry(entry),
                supports_vision=parse_vision_override(entry),
            )
    return FeatureModelsPreference(
        overrides=overrides,
        known_keys=list(GLOBAL_FEATURE_KEYS),
        labels=dict(FEATURE_LABELS),
    )


@router.put(
    "/system/preferences/feature-models",
    response_model=FeatureModelsPreference,
)
async def set_feature_model_preferences(
    payload: FeatureModelsPreference,
    scope: str = Query(default="global"),
    container: ServiceContainer = Depends(get_container),
    current_user: OperatorProfile = Depends(require_admin),
) -> FeatureModelsPreference:
    """Persist per-feature overrides (full replace).

    Unknown keys in the payload are silently dropped — protects against
    frontend drift / typos writing garbage into the pref blob. An
    entry with all fields null means "clear this feature's override",
    same effect as omitting it from the payload.
    """
    cleaned: dict[str, dict[str, Any]] = {}
    for key, entry in payload.overrides.items():
        if key not in GLOBAL_FEATURE_KEYS:
            continue
        value = _cleaned_routing_entry(entry)
        if value is None:
            # All-null entry → pretend the key wasn't sent.
            continue
        cleaned[key] = value
    user_id = _preference_user_id(scope, current_user)
    await _validate_feature_reasoning_efforts(
        container,
        cleaned,
        user_id=user_id,
    )
    if user_id and not cleaned:
        await delete_user_preference(
            container.preferences_repository,
            _FEATURE_MODELS_KEY,
            user_id=user_id,
        )
    else:
        await _set_preference(
            container,
            _FEATURE_MODELS_KEY,
            cleaned,
            user_id=user_id,
        )
    # Return the sanitised view so the UI can reconcile what actually
    # got saved (drops unknown keys + blank entries).
    return FeatureModelsPreference(
        overrides={
            k: FeatureModelEntry(**v) for k, v in cleaned.items()
        },
        known_keys=list(GLOBAL_FEATURE_KEYS),
        labels=dict(FEATURE_LABELS),
    )


@router.get(
    "/system/preferences/feature-model-groups",
    response_model=FeatureModelGroupsPreference,
)
async def get_feature_model_group_preferences(
    scope: str = Query(default="global"),
    container: ServiceContainer = Depends(get_container),
    current_user: OperatorProfile = Depends(get_current_user),
) -> FeatureModelGroupsPreference:
    user_id = _read_preference_user_id(scope, current_user)
    raw_groups = await _get_preference(
        container,
        _FEATURE_MODEL_GROUPS_KEY,
        user_id=user_id,
    )
    overrides: dict[str, FeatureModelEntry] = {}
    if isinstance(raw_groups, dict):
        for key, entry in raw_groups.items():
            if key not in LLM_FEATURE_GROUP_KEYS or not isinstance(entry, dict):
                continue
            overrides[key] = FeatureModelEntry(
                provider_id=_coerce_str(entry.get("provider_id")),
                model_id=_coerce_str(entry.get("model_id")),
                reasoning=_reasoning_from_raw_entry(entry),
                supports_vision=parse_vision_override(entry),
            )

    raw_active = await _get_preference(
        container,
        _ACTIVE_MODEL_KEY,
        user_id=user_id,
    )
    active_model = ActiveModelPreference()
    if isinstance(raw_active, dict):
        active_model = ActiveModelPreference(
            provider_id=_coerce_str(raw_active.get("provider_id")),
            model_id=_coerce_str(raw_active.get("model_id")),
            supports_vision=parse_vision_override(raw_active),
        )

    return FeatureModelGroupsPreference(
        groups=[
            FeatureModelGroupSummary(
                key=group_key,
                label=FEATURE_GROUP_LABELS[group_key],
                description=FEATURE_GROUP_DESCRIPTIONS[group_key],
                model_guidance=FEATURE_GROUP_MODEL_GUIDANCE[group_key],
                members=[
                    FeatureGroupMember(
                        key=feature_key,
                        label=FEATURE_LABELS.get(feature_key, feature_key),
                    )
                    for feature_key in FEATURE_GROUP_MEMBERS[group_key]
                ],
                model=overrides.get(group_key),
            )
            for group_key in LLM_FEATURE_GROUP_KEYS
        ],
        active_model=active_model,
    )


@router.put(
    "/system/preferences/feature-model-groups",
    response_model=FeatureModelGroupsPreference,
)
async def set_feature_model_group_preferences(
    payload: FeatureModelGroupsUpdate,
    scope: str = Query(default="global"),
    container: ServiceContainer = Depends(get_container),
    current_user: OperatorProfile = Depends(require_admin),
) -> FeatureModelGroupsPreference:
    cleaned: dict[str, dict[str, Any]] = {}
    for key, entry in payload.feature_model_groups.items():
        if key not in LLM_FEATURE_GROUP_KEYS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"unknown feature model group: {key!r}",
            )
        value = _cleaned_routing_entry(entry)
        if value is None:
            continue
        cleaned[key] = value
    user_id = _preference_user_id(scope, current_user)
    await _validate_group_reasoning_efforts(
        container,
        cleaned,
        user_id=user_id,
    )
    if user_id and not cleaned:
        await delete_user_preference(
            container.preferences_repository,
            _FEATURE_MODEL_GROUPS_KEY,
            user_id=user_id,
        )
    else:
        await _set_preference(
            container,
            _FEATURE_MODEL_GROUPS_KEY,
            cleaned,
            user_id=user_id,
        )
    return await get_feature_model_group_preferences(
        scope=scope,
        container=container,
        current_user=current_user,
    )


@router.get(
    "/system/preferences/tts-pregeneration",
    response_model=TTSPregenerationPreference,
)
async def get_tts_pregeneration_preference(
    scope: str = Query(default="user"),
    container: ServiceContainer = Depends(get_container),
    current_user: OperatorProfile = Depends(get_current_user),
) -> TTSPregenerationPreference:
    user_id = _preference_user_id(scope, current_user)
    service = container.tts_pregeneration_service
    if service is not None:
        return TTSPregenerationPreference(
            enabled=await service.is_enabled(user_id=user_id),
        )
    raw = await _get_preference(
        container,
        _TTS_PREGENERATION_KEY,
        user_id=user_id,
    )
    if isinstance(raw, dict):
        return TTSPregenerationPreference(enabled=bool(raw.get("enabled", False)))
    if isinstance(raw, bool):
        return TTSPregenerationPreference(enabled=raw)
    return TTSPregenerationPreference()


@router.put(
    "/system/preferences/tts-pregeneration",
    response_model=TTSPregenerationPreference,
)
async def set_tts_pregeneration_preference(
    payload: TTSPregenerationPreference,
    scope: str = Query(default="user"),
    container: ServiceContainer = Depends(get_container),
    current_user: OperatorProfile = Depends(get_current_user),
) -> TTSPregenerationPreference:
    user_id = _preference_user_id(scope, current_user)
    service = container.tts_pregeneration_service
    if service is not None:
        enabled = await service.set_enabled(payload.enabled, user_id=user_id)
    else:
        enabled = bool(payload.enabled)
        await _set_preference(
            container,
            _TTS_PREGENERATION_KEY,
            {"enabled": enabled},
            user_id=user_id,
        )
    return TTSPregenerationPreference(enabled=enabled)


@router.get(
    "/system/preferences/chat-assist",
    response_model=ChatAssistPreference,
)
async def get_chat_assist_preference(
    scope: str = Query(default="user"),
    container: ServiceContainer = Depends(get_container),
    current_user: OperatorProfile = Depends(get_current_user),
) -> ChatAssistPreference:
    user_id = _preference_user_id(scope, current_user)
    raw = await _get_preference(
        container,
        _CHAT_ASSIST_KEY,
        user_id=user_id,
    )
    if isinstance(raw, dict):
        return ChatAssistPreference(enabled=bool(raw.get("enabled", True)))
    if isinstance(raw, bool):
        return ChatAssistPreference(enabled=raw)
    return ChatAssistPreference()


@router.put(
    "/system/preferences/chat-assist",
    response_model=ChatAssistPreference,
)
async def set_chat_assist_preference(
    payload: ChatAssistPreference,
    scope: str = Query(default="user"),
    container: ServiceContainer = Depends(get_container),
    current_user: OperatorProfile = Depends(get_current_user),
) -> ChatAssistPreference:
    user_id = _preference_user_id(scope, current_user)
    enabled = bool(payload.enabled)
    await _set_preference(
        container,
        _CHAT_ASSIST_KEY,
        {"enabled": enabled},
        user_id=user_id,
    )
    return ChatAssistPreference(enabled=enabled)


@router.get(
    "/system/preferences/visual-generation-style",
    response_model=VisualGenerationStylePreference,
)
async def get_visual_generation_style_preference(
    scope: str = Query(default="user"),
    container: ServiceContainer = Depends(get_container),
    current_user: OperatorProfile = Depends(get_current_user),
) -> VisualGenerationStylePreference:
    user_id = _preference_user_id(scope, current_user)
    raw = await _get_preference(
        container,
        _VISUAL_GENERATION_STYLE_KEY,
        user_id=user_id,
    )
    return VisualGenerationStylePreference(
        style=normalise_visual_generation_style(raw),
    )


@router.put(
    "/system/preferences/visual-generation-style",
    response_model=VisualGenerationStylePreference,
)
async def set_visual_generation_style_preference(
    payload: VisualGenerationStylePreference,
    scope: str = Query(default="user"),
    container: ServiceContainer = Depends(get_container),
    current_user: OperatorProfile = Depends(get_current_user),
) -> VisualGenerationStylePreference:
    if not is_supported_visual_generation_style(payload.style):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="style must be 'anime' or 'realistic'",
        )
    user_id = _preference_user_id(scope, current_user)
    style = normalise_visual_generation_style(payload.style)
    await _set_preference(
        container,
        _VISUAL_GENERATION_STYLE_KEY,
        {"style": style},
        user_id=user_id,
    )
    return VisualGenerationStylePreference(style=style)


def _require_quiet_hours_service(container: ServiceContainer):
    service = getattr(container, "quiet_hours_service", None)
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="quiet hours service not configured",
        )
    return service


@router.get(
    "/system/preferences/quiet-hours",
    response_model=QuietHoursPreference,
)
async def get_quiet_hours_preference(
    scope: str = Query(default="user"),
    container: ServiceContainer = Depends(get_container),
    current_user: OperatorProfile = Depends(get_current_user),
) -> QuietHoursPreference:
    """Return the caller's quiet-hours window.

    ``scope=user`` (default) resolves user override → global → env;
    ``scope=global`` returns the installation-wide default without a
    user lookup and is admin-only — same semantics as the active-model
    / tts-pregeneration preferences.
    """
    user_id = _preference_user_id(scope, current_user)
    service = _require_quiet_hours_service(container)
    window = await service.window(user_id=user_id)
    return QuietHoursPreference(start=window.start, end=window.end)


@router.put(
    "/system/preferences/quiet-hours",
    response_model=QuietHoursPreference,
)
async def set_quiet_hours_preference(
    payload: QuietHoursPreference,
    scope: str = Query(default="user"),
    container: ServiceContainer = Depends(get_container),
    current_user: OperatorProfile = Depends(get_current_user),
) -> QuietHoursPreference:
    """Persist a quiet-hours window. ``scope=user`` writes a per-user
    override; ``scope=global`` updates the installation-wide default
    and is admin-only.

    Either bound may equal the other (zero-length window = "never
    quiet"); ``start > end`` legitimately represents a wrap-around
    window like 23–07. The service clamps both bounds to 0–23."""
    user_id = _preference_user_id(scope, current_user)
    service = _require_quiet_hours_service(container)
    window = await service.set_window(
        start=payload.start, end=payload.end, user_id=user_id,
    )
    return QuietHoursPreference(start=window.start, end=window.end)


@router.delete(
    "/system/preferences/quiet-hours",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def clear_quiet_hours_preference(
    container: ServiceContainer = Depends(get_container),
    current_user: OperatorProfile = Depends(get_current_user),
) -> None:
    """Drop the caller's user override so the global default applies
    again. Idempotent — clearing an absent override is a no-op."""
    service = _require_quiet_hours_service(container)
    await service.clear_user_window(user_id=current_user.id)


def _coerce_str(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    return None


def _normalized_scope(scope: str) -> str:
    normalized = scope.strip().lower()
    if normalized not in ("user", "global"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="scope must be 'user' or 'global'",
        )
    return normalized


def _preference_user_id(scope: str, user: OperatorProfile) -> str | None:
    """Write-side scope resolution: ``global`` is admin-only.

    Defence in depth behind ``require_admin`` on the routing PUTs, and
    the live gate on the player-pref endpoints (whose GETs also stay
    strict — players have no reason to read another surface's global
    row when the fallback already answers)."""
    if _normalized_scope(scope) == "user":
        return user.id
    if user.is_admin:
        return None
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="admin privilege required",
    )


def _read_preference_user_id(scope: str, user: OperatorProfile) -> str | None:
    """Read-side scope resolution for the ROUTING GETs: ``global`` is
    readable by any signed-in user.

    Fresh accounts already see the global values through the user
    fallback, so the explicit read leaks nothing new — and with
    ``global`` as the routing scope default, keeping the admin check
    here would 403 every scope-less player read that the read-only
    surfaces depend on."""
    return user.id if _normalized_scope(scope) == "user" else None


async def _get_preference(
    container: ServiceContainer,
    key: str,
    *,
    user_id: str | None,
) -> Any:
    if user_id:
        return await get_preference_with_user_fallback(
            container.preferences_repository,
            key,
            user_id=user_id,
        )
    return await container.preferences_repository.get(key)


async def _set_preference(
    container: ServiceContainer,
    key: str,
    value: object,
    *,
    user_id: str | None,
) -> None:
    if user_id:
        await set_user_preference(
            container.preferences_repository,
            key,
            value,
            user_id=user_id,
        )
        return
    await container.preferences_repository.set(key, value)


# ----------------------------------------------------------------------
# Image profile preferences (parallel to active-model / feature-models).
# Same fall-through semantics: per-character override → per-feature
# global override → active_image_profile → first registered profile.
# ----------------------------------------------------------------------


class ImageProfileSummary(BaseModel):
    """One row in ``GET /system/image-profiles``.

    ``kind`` is ``comfyui`` or ``openai``. The UI uses it to pick which
    sub-fields (checkpoint vs. quality) to surface in tooltips; the
    backend doesn't care once the profile is built."""

    id: str
    label: str
    kind: str


class ActiveImageProfilePreference(BaseModel):
    profile_id: str | None = None


class ImageFeatureEntry(BaseModel):
    profile_id: str | None = None


class ImageFeatureProfilesPreference(BaseModel):
    overrides: dict[str, ImageFeatureEntry] = Field(default_factory=dict)
    known_keys: list[str] = Field(default_factory=list)
    labels: dict[str, str] = Field(default_factory=dict)


@router.get("/system/image-profiles", response_model=list[ImageProfileSummary])
async def list_image_profiles(
    container: ServiceContainer = Depends(get_container),
) -> list[ImageProfileSummary]:
    """Return every operator-defined image profile in declaration order.

    The first row is the implicit default the resolver falls back to
    when no preference matches — operators control that by ordering
    their ``KOKORO_IMAGE_PROFILES`` JSON. Empty list = no profiles
    configured; the UI should disable the picker and show a hint."""
    return [
        ImageProfileSummary(id=p.id, label=p.label, kind=p.kind)
        for p in container.image_profile_registry.profiles
    ]


class ComfyCheckpointList(BaseModel):
    """Result of ``GET /system/comfyui/checkpoints``.

    ``available`` is False (with an ``error`` string) when the ComfyUI
    ``/object_info`` fetch failed — the admin form uses that to fall back
    to a plain text input for the checkpoint field rather than blocking
    the whole provider form (see CORE_ENV_TO_ADMIN_CONFIG plan risk note).
    """

    available: bool = False
    checkpoints: list[str] = Field(default_factory=list)
    error: str = ""


@router.get(
    "/system/comfyui/checkpoints",
    response_model=ComfyCheckpointList,
)
async def list_comfyui_checkpoints(
    server: str = Query(default=""),
) -> ComfyCheckpointList:
    """List checkpoint files an operator's ComfyUI advertises.

    Powers the checkpoint dropdown in the ComfyUI provider form. Takes
    ``server`` as a query param (the form value, possibly for an unsaved
    row). Never raises: an unreachable / malformed ComfyUI degrades to
    ``available=False`` so the UI falls back to free-text entry."""
    target = server.strip()
    if not target:
        return ComfyCheckpointList(
            available=False, error="server URL is required",
        )
    from kokoro_link.infrastructure.tools.comfyui.client import (
        AsyncComfyUiClient,
        ComfyUiError,
    )

    client = AsyncComfyUiClient(server=target, http_timeout=8.0)
    try:
        names = await client.list_checkpoints()
    except (ComfyUiError, Exception) as exc:  # fail-soft → text fallback
        return ComfyCheckpointList(available=False, error=str(exc))
    return ComfyCheckpointList(available=True, checkpoints=names)


@router.get(
    "/system/preferences/active-image-profile",
    response_model=ActiveImageProfilePreference,
)
async def get_active_image_profile_preference(
    scope: str = Query(default="global"),
    container: ServiceContainer = Depends(get_container),
    current_user: OperatorProfile = Depends(get_current_user),
) -> ActiveImageProfilePreference:
    user_id = _read_preference_user_id(scope, current_user)
    raw = await _get_preference(container, _ACTIVE_IMAGE_KEY, user_id=user_id)
    if not isinstance(raw, dict):
        return ActiveImageProfilePreference()
    return ActiveImageProfilePreference(
        profile_id=_coerce_str(raw.get("profile_id")),
    )


@router.put(
    "/system/preferences/active-image-profile",
    response_model=ActiveImageProfilePreference,
)
async def set_active_image_profile_preference(
    payload: ActiveImageProfilePreference,
    scope: str = Query(default="global"),
    container: ServiceContainer = Depends(get_container),
    current_user: OperatorProfile = Depends(require_admin),
) -> ActiveImageProfilePreference:
    """Persist the global image profile pick. ``profile_id=null`` clears
    the pref, falling generation back through the per-feature picks /
    first registered profile."""
    profile_id = _coerce_str(payload.profile_id)
    if profile_id is not None:
        if container.image_profile_registry.get_profile(profile_id) is None:
            raise HTTPException(
                status_code=400,
                detail=f"unknown image profile id: {profile_id!r}",
            )
    user_id = _preference_user_id(scope, current_user)
    if user_id and profile_id is None:
        await delete_user_preference(
            container.preferences_repository,
            _ACTIVE_IMAGE_KEY,
            user_id=user_id,
        )
    else:
        await _set_preference(
            container,
            _ACTIVE_IMAGE_KEY,
            {"profile_id": profile_id},
            user_id=user_id,
        )
    return ActiveImageProfilePreference(profile_id=profile_id)


@router.get(
    "/system/preferences/image-feature-profiles",
    response_model=ImageFeatureProfilesPreference,
)
async def get_image_feature_preferences(
    scope: str = Query(default="global"),
    container: ServiceContainer = Depends(get_container),
    current_user: OperatorProfile = Depends(get_current_user),
) -> ImageFeatureProfilesPreference:
    user_id = _read_preference_user_id(scope, current_user)
    raw = await _get_preference(container, _IMAGE_FEATURE_KEY, user_id=user_id)
    overrides: dict[str, ImageFeatureEntry] = {}
    if isinstance(raw, dict):
        for key, entry in raw.items():
            if key not in IMAGE_FEATURE_KEYS:
                continue
            if not isinstance(entry, dict):
                continue
            overrides[key] = ImageFeatureEntry(
                profile_id=_coerce_str(entry.get("profile_id")),
            )
    return ImageFeatureProfilesPreference(
        overrides=overrides,
        known_keys=list(IMAGE_FEATURE_KEYS),
        labels={k: FEATURE_LABELS.get(k, k) for k in IMAGE_FEATURE_KEYS},
    )


@router.put(
    "/system/preferences/image-feature-profiles",
    response_model=ImageFeatureProfilesPreference,
)
async def set_image_feature_preferences(
    payload: ImageFeatureProfilesPreference,
    scope: str = Query(default="global"),
    container: ServiceContainer = Depends(get_container),
    current_user: OperatorProfile = Depends(require_admin),
) -> ImageFeatureProfilesPreference:
    """Full-replace persist of the per-feature image profile picks.

    Unknown feature keys and unknown profile ids are silently dropped
    so a stale frontend can't poison the pref blob. An entry with a
    null ``profile_id`` is treated as "clear this feature's override"."""
    cleaned: dict[str, dict[str, str | None]] = {}
    for key, entry in payload.overrides.items():
        if key not in IMAGE_FEATURE_KEYS:
            continue
        profile_id = _coerce_str(entry.profile_id)
        if profile_id is None:
            continue
        if container.image_profile_registry.get_profile(profile_id) is None:
            continue
        cleaned[key] = {"profile_id": profile_id}
    user_id = _preference_user_id(scope, current_user)
    if user_id and not cleaned:
        await delete_user_preference(
            container.preferences_repository,
            _IMAGE_FEATURE_KEY,
            user_id=user_id,
        )
    else:
        await _set_preference(
            container,
            _IMAGE_FEATURE_KEY,
            cleaned,
            user_id=user_id,
        )
    return ImageFeatureProfilesPreference(
        overrides={
            k: ImageFeatureEntry(profile_id=v["profile_id"])
            for k, v in cleaned.items()
        },
        known_keys=list(IMAGE_FEATURE_KEYS),
        labels={k: FEATURE_LABELS.get(k, k) for k in IMAGE_FEATURE_KEYS},
    )


# ----------------------------------------------------------------------
# Video profile preferences (parallel to active-image / image-feature
# routes). Same fall-through semantics, different feature key set.
# ----------------------------------------------------------------------


class VideoProfileSummary(BaseModel):
    """One row in ``GET /system/video-profiles``.

    ``kind`` is currently always ``comfyui_wan22``; carried in the
    payload so the UI can label rows the same way it does on the image
    side and so new backends slot in without an API shape change."""

    id: str
    label: str
    kind: str


class ActiveVideoProfilePreference(BaseModel):
    profile_id: str | None = None


class VideoFeatureEntry(BaseModel):
    profile_id: str | None = None


class VideoFeatureProfilesPreference(BaseModel):
    overrides: dict[str, VideoFeatureEntry] = Field(default_factory=dict)
    known_keys: list[str] = Field(default_factory=list)
    labels: dict[str, str] = Field(default_factory=dict)


@router.get("/system/video-profiles", response_model=list[VideoProfileSummary])
async def list_video_profiles(
    container: ServiceContainer = Depends(get_container),
) -> list[VideoProfileSummary]:
    """Return operator-defined video profiles in declaration order.

    Empty list = video generation off for this deployment (no
    ``KOKORO_VIDEO_PROFILES`` configured); the UI should grey out the
    picker and the feed composer skips video automatically."""
    return [
        VideoProfileSummary(id=p.id, label=p.label, kind=p.kind)
        for p in container.video_profile_registry.profiles
    ]


@router.get(
    "/system/preferences/active-video-profile",
    response_model=ActiveVideoProfilePreference,
)
async def get_active_video_profile_preference(
    scope: str = Query(default="global"),
    container: ServiceContainer = Depends(get_container),
    current_user: OperatorProfile = Depends(get_current_user),
) -> ActiveVideoProfilePreference:
    user_id = _read_preference_user_id(scope, current_user)
    raw = await _get_preference(container, _ACTIVE_VIDEO_KEY, user_id=user_id)
    if not isinstance(raw, dict):
        return ActiveVideoProfilePreference()
    return ActiveVideoProfilePreference(
        profile_id=_coerce_str(raw.get("profile_id")),
    )


@router.put(
    "/system/preferences/active-video-profile",
    response_model=ActiveVideoProfilePreference,
)
async def set_active_video_profile_preference(
    payload: ActiveVideoProfilePreference,
    scope: str = Query(default="global"),
    container: ServiceContainer = Depends(get_container),
    current_user: OperatorProfile = Depends(require_admin),
) -> ActiveVideoProfilePreference:
    profile_id = _coerce_str(payload.profile_id)
    if profile_id is not None:
        if container.video_profile_registry.get_profile(profile_id) is None:
            raise HTTPException(
                status_code=400,
                detail=f"unknown video profile id: {profile_id!r}",
            )
    user_id = _preference_user_id(scope, current_user)
    if user_id and profile_id is None:
        await delete_user_preference(
            container.preferences_repository,
            _ACTIVE_VIDEO_KEY,
            user_id=user_id,
        )
    else:
        await _set_preference(
            container,
            _ACTIVE_VIDEO_KEY,
            {"profile_id": profile_id},
            user_id=user_id,
        )
    return ActiveVideoProfilePreference(profile_id=profile_id)


@router.get(
    "/system/preferences/video-feature-profiles",
    response_model=VideoFeatureProfilesPreference,
)
async def get_video_feature_preferences(
    scope: str = Query(default="global"),
    container: ServiceContainer = Depends(get_container),
    current_user: OperatorProfile = Depends(get_current_user),
) -> VideoFeatureProfilesPreference:
    user_id = _read_preference_user_id(scope, current_user)
    raw = await _get_preference(container, _VIDEO_FEATURE_KEY, user_id=user_id)
    overrides: dict[str, VideoFeatureEntry] = {}
    if isinstance(raw, dict):
        for key, entry in raw.items():
            if key not in VIDEO_FEATURE_KEYS:
                continue
            if not isinstance(entry, dict):
                continue
            overrides[key] = VideoFeatureEntry(
                profile_id=_coerce_str(entry.get("profile_id")),
            )
    return VideoFeatureProfilesPreference(
        overrides=overrides,
        known_keys=list(VIDEO_FEATURE_KEYS),
        labels={k: FEATURE_LABELS.get(k, k) for k in VIDEO_FEATURE_KEYS},
    )


@router.put(
    "/system/preferences/video-feature-profiles",
    response_model=VideoFeatureProfilesPreference,
)
async def set_video_feature_preferences(
    payload: VideoFeatureProfilesPreference,
    scope: str = Query(default="global"),
    container: ServiceContainer = Depends(get_container),
    current_user: OperatorProfile = Depends(require_admin),
) -> VideoFeatureProfilesPreference:
    cleaned: dict[str, dict[str, str | None]] = {}
    for key, entry in payload.overrides.items():
        if key not in VIDEO_FEATURE_KEYS:
            continue
        profile_id = _coerce_str(entry.profile_id)
        if profile_id is None:
            continue
        if container.video_profile_registry.get_profile(profile_id) is None:
            continue
        cleaned[key] = {"profile_id": profile_id}
    user_id = _preference_user_id(scope, current_user)
    if user_id and not cleaned:
        await delete_user_preference(
            container.preferences_repository,
            _VIDEO_FEATURE_KEY,
            user_id=user_id,
        )
    else:
        await _set_preference(
            container,
            _VIDEO_FEATURE_KEY,
            cleaned,
            user_id=user_id,
        )
    return VideoFeatureProfilesPreference(
        overrides={
            k: VideoFeatureEntry(profile_id=v["profile_id"])
            for k, v in cleaned.items()
        },
        known_keys=list(VIDEO_FEATURE_KEYS),
        labels={k: FEATURE_LABELS.get(k, k) for k in VIDEO_FEATURE_KEYS},
    )
