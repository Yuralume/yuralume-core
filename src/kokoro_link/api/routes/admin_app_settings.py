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
from kokoro_link.bootstrap.site_settings_holder import SITE_SETTINGS_HOT_GROUPS
from kokoro_link.contracts.runtime_config_events import (
    RUNTIME_CONFIG_TOPIC_SITE_SETTINGS,
)
from kokoro_link.infrastructure.app_runtime_settings.schemas import (
    APP_SETTINGS_GROUPS,
)
from kokoro_link.infrastructure.persistence.runtime_config_signal import (
    notify_runtime_config_changed,
)

router = APIRouter(
    prefix="/admin/app-settings",
    tags=["admin-app-settings"],
    dependencies=[Depends(require_admin)],
)


def _service(container: ServiceContainer) -> AppRuntimeSettingsService:
    """Build the service with the "converge everyone" hook attached.

    Before this, a PUT here only wrote the row: the replica that served the
    request re-read nothing (its own site settings were a boot-time snapshot
    too) and the other Hosted processes never learned at all, so changing the
    site weather coordinates needed a rolling restart of the whole fleet.

    The hook mirrors ``admin_providers._apply_provider_change``: reload THIS
    process first, then tell the others. Self-host gets the local half and no
    transport (the NOTIFY is an inert no-op off PostgreSQL) — which is exactly
    right, because a single process *is* everyone.
    """
    engine = getattr(container, "db_engine", None)
    reload_local = getattr(container, "site_settings_reloader", None)

    async def _converge(group: str) -> None:
        # Only the hot groups are held in memory anywhere; the rest are read
        # on demand, so waking the fleet for them would be pure noise.
        if group not in SITE_SETTINGS_HOT_GROUPS:
            return
        if reload_local is not None:
            await reload_local()
        await notify_runtime_config_changed(
            engine, topic=RUNTIME_CONFIG_TOPIC_SITE_SETTINGS,
        )

    return AppRuntimeSettingsService(
        container.runtime_settings_repository, on_changed=_converge,
    )


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
