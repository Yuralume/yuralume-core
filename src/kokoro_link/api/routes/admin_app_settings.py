"""Admin routes for site-level runtime settings (CORE_ENV_TO_ADMIN_CONFIG track 2).

``GET  /admin/app-settings``          → group catalog + JSON schema per group
``GET  /admin/app-settings/{group}``  → current values (DB → env fallback)
``PUT  /admin/app-settings/{group}``  → validate + persist a group

The Admin「站點設定」page renders a form per group from the JSON schema and
writes back here. Validation (lat/lon pairing, TTL floors) lives in the
pydantic schema; a failure returns 400 with the pydantic message.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status

from kokoro_link.api.dependencies import get_container, require_admin
from kokoro_link.application.services.app_runtime_settings_service import (
    AppRuntimeSettingsError,
    AppRuntimeSettingsService,
)
from kokoro_link.bootstrap.container import ServiceContainer
from kokoro_link.bootstrap.app_runtime_settings_seed import env_default_for_group
from kokoro_link.infrastructure.app_runtime_settings.schemas import (
    APP_SETTINGS_GROUPS,
)

router = APIRouter(
    prefix="/admin/app-settings",
    tags=["admin-app-settings"],
    dependencies=[Depends(require_admin)],
)


def _service(container: ServiceContainer) -> AppRuntimeSettingsService:
    return AppRuntimeSettingsService(container.runtime_settings_repository)


@router.get("")
async def list_app_settings_groups(
    container: ServiceContainer = Depends(get_container),
) -> dict[str, Any]:
    """Return the group catalog with each group's JSON schema.

    The frontend builds a form per group from the schema (field kinds,
    defaults, constraints) so adding a new group needs no UI change."""
    return {
        "groups": [
            {"group": name, "schema": schema.model_json_schema()}
            for name, schema in APP_SETTINGS_GROUPS.items()
        ],
    }


@router.get("/{group}")
async def get_app_settings_group(
    group: str,
    container: ServiceContainer = Depends(get_container),
) -> dict[str, Any]:
    if group not in APP_SETTINGS_GROUPS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown group: {group}")
    service = _service(container)
    default = env_default_for_group(group, container.app_settings)
    config = await service.get(group, default=default)
    return {"group": group, "values": config.model_dump()}


@router.put("/{group}")
async def set_app_settings_group(
    group: str,
    payload: dict[str, Any],
    container: ServiceContainer = Depends(get_container),
) -> dict[str, Any]:
    if group not in APP_SETTINGS_GROUPS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown group: {group}")
    service = _service(container)
    try:
        config = await service.set(group, payload)
    except AppRuntimeSettingsError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, str(exc),
        ) from exc
    return {"group": group, "values": config.model_dump()}
