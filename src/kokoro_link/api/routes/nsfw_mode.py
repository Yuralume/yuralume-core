"""NSFW mode preference API.

The mode is explicit user state for self-host installs.  It uses the
admin-configured community LLM/image targets that the routing overlay should
use while active; it does not infer or classify message content.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from kokoro_link.api.dependencies import (
    get_container,
    get_current_user,
    is_cloud_mode,
    require_admin,
)
from kokoro_link.api.routes.system import FeatureReasoningOverride
from kokoro_link.application.services.nsfw_mode import (
    NsfwModeService,
    NsfwModeStatus,
    NsfwModeTarget,
    NsfwModeTargetError,
)
from kokoro_link.application.services.routing_reasoning import (
    reasoning_override_from_fields,
)
from kokoro_link.application.services.routing_reasoning_validation import (
    ReasoningEffortValidationError,
    RoutingReasoningValidationService,
)
from kokoro_link.bootstrap.container import ServiceContainer
from kokoro_link.contracts.llm import ReasoningOverrides
from kokoro_link.domain.entities.operator_profile import OperatorProfile

router = APIRouter(tags=["system"])


class NsfwModeTargetPayload(BaseModel):
    llm_provider_id: str = Field(min_length=1)
    llm_model_id: str = Field(min_length=1)
    # JSON null = the operator explicitly disabled image generation while
    # the mode is active (no fallback to a normal profile), not "unset".
    image_profile_id: str | None = Field(default=None, min_length=1)
    # Reasoning posture for the NSFW target itself — the normal route's
    # feature/group postures never ride onto the reroute. ``null`` /
    # all-default = keep the target connection's reasoning defaults.
    reasoning: FeatureReasoningOverride | None = None


class NsfwModePreferenceUpdate(BaseModel):
    active: bool


class NsfwModePreference(BaseModel):
    active: bool
    configured: bool
    locked: bool = False
    ttl_seconds: int
    last_activity_at: datetime | None = None
    expires_at: datetime | None = None
    target: NsfwModeTargetPayload | None = None


class NsfwModeTargetPreference(BaseModel):
    configured: bool
    locked: bool = False
    target: NsfwModeTargetPayload | None = None


@router.get(
    "/system/preferences/nsfw-mode",
    response_model=NsfwModePreference,
)
async def get_nsfw_mode_preference(
    container: ServiceContainer = Depends(get_container),
    current_user: OperatorProfile = Depends(get_current_user),
) -> NsfwModePreference:
    service = _require_service(container)
    return _to_response(
        await service.get_status(user_id=current_user.id),
        locked=is_cloud_mode(container),
    )


@router.put(
    "/system/preferences/nsfw-mode",
    response_model=NsfwModePreference,
)
async def set_nsfw_mode_preference(
    payload: NsfwModePreferenceUpdate,
    container: ServiceContainer = Depends(get_container),
    current_user: OperatorProfile = Depends(get_current_user),
) -> NsfwModePreference:
    if is_cloud_mode(container):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="nsfw mode is locked in cloud mode",
        )
    service = _require_service(container)
    if not payload.active:
        return _to_response(
            await service.disable(user_id=current_user.id),
            locked=False,
        )
    configured_target = await service.configured_target(user_id=current_user.id)
    if configured_target is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="nsfw mode requires an admin-configured target",
        )
    _validate_target(_target_to_payload(configured_target), container=container)
    try:
        result = await service.enable(
            user_id=current_user.id,
        )
    except NsfwModeTargetError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    return _to_response(result, locked=False)


@router.get(
    "/admin/system/preferences/nsfw-mode-target",
    response_model=NsfwModeTargetPreference,
)
async def get_admin_nsfw_mode_target(
    container: ServiceContainer = Depends(get_container),
    _: OperatorProfile = Depends(require_admin),
) -> NsfwModeTargetPreference:
    service = _require_service(container)
    return _target_preference_response(
        target=await service.get_global_target(),
        locked=is_cloud_mode(container),
    )


@router.put(
    "/admin/system/preferences/nsfw-mode-target",
    response_model=NsfwModeTargetPreference,
)
async def set_admin_nsfw_mode_target(
    payload: NsfwModeTargetPayload,
    container: ServiceContainer = Depends(get_container),
    _: OperatorProfile = Depends(require_admin),
) -> NsfwModeTargetPreference:
    if is_cloud_mode(container):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="nsfw mode is locked in cloud mode",
        )
    service = _require_service(container)
    _validate_target(payload, container=container)
    reasoning = _reasoning_from_payload(payload.reasoning)
    await _preflight_reasoning_effort(
        reasoning,
        provider_id=payload.llm_provider_id,
        model_id=payload.llm_model_id,
        container=container,
    )
    try:
        target = await service.set_global_target(
            llm_provider_id=payload.llm_provider_id,
            llm_model_id=payload.llm_model_id,
            image_profile_id=payload.image_profile_id,
            reasoning=reasoning,
        )
    except NsfwModeTargetError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    return _target_preference_response(target=target, locked=False)


def _require_service(container: ServiceContainer) -> NsfwModeService:
    service = getattr(container, "nsfw_mode_service", None)
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="nsfw mode service not configured",
        )
    return service


def _validate_target(
    target: NsfwModeTargetPayload,
    *,
    container: ServiceContainer,
) -> None:
    if target.llm_provider_id not in container.model_registry.list_ids():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"unknown llm provider id: {target.llm_provider_id!r}",
        )
    if (
        target.image_profile_id is not None
        and container.image_profile_registry.get_profile(target.image_profile_id)
        is None
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"unknown image profile id: {target.image_profile_id!r}",
        )


def _reasoning_from_payload(
    reasoning: FeatureReasoningOverride | None,
) -> ReasoningOverrides | None:
    """Normalise the submitted posture — all-default collapses to None
    (matching the routing-entry write-side rule)."""
    if reasoning is None:
        return None
    return reasoning_override_from_fields(
        disable_reasoning=reasoning.disable_reasoning,
        reasoning_effort=reasoning.reasoning_effort,
        thinking_budget_tokens=reasoning.thinking_budget_tokens,
    )


async def _preflight_reasoning_effort(
    reasoning: ReasoningOverrides | None,
    *,
    provider_id: str,
    model_id: str,
    container: ServiceContainer,
) -> None:
    """Probe a free-text effort against the real target upstream before
    persisting — same live preflight as the feature/group entries."""
    if reasoning is None or reasoning.reasoning_effort is None:
        return
    validator = RoutingReasoningValidationService(container.model_registry)
    try:
        await validator.validate(
            provider_id=provider_id,
            model_id=model_id,
            effort=reasoning.reasoning_effort,
        )
    except ReasoningEffortValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc


def _target_to_payload(target: NsfwModeTarget) -> NsfwModeTargetPayload:
    reasoning = target.reasoning
    return NsfwModeTargetPayload(
        llm_provider_id=target.llm_provider_id,
        llm_model_id=target.llm_model_id,
        image_profile_id=target.image_profile_id,
        reasoning=(
            FeatureReasoningOverride(
                disable_reasoning=reasoning.disable_reasoning,
                reasoning_effort=reasoning.reasoning_effort,
                thinking_budget_tokens=reasoning.thinking_budget_tokens,
            )
            if reasoning is not None
            else None
        ),
    )


def _target_preference_response(
    *,
    target: NsfwModeTarget | None,
    locked: bool,
) -> NsfwModeTargetPreference:
    return NsfwModeTargetPreference(
        configured=target is not None,
        locked=locked,
        target=_target_to_payload(target) if target is not None else None,
    )


def _to_response(
    status_value: NsfwModeStatus,
    *,
    locked: bool,
) -> NsfwModePreference:
    target = status_value.configured_target
    return NsfwModePreference(
        active=status_value.active,
        configured=status_value.configured,
        locked=locked,
        ttl_seconds=status_value.ttl_seconds,
        last_activity_at=status_value.last_activity_at,
        expires_at=status_value.expires_at,
        target=_target_to_payload(target) if target is not None else None,
    )
