"""Admin BYOK provider settings routes."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from kokoro_link.api.dependencies import get_container, is_cloud_mode, require_admin
from kokoro_link.application.services.provider_connection_service import (
    ProviderConnectionError,
    ProviderConnectionService,
    ProviderConnectionTestResult,
    ProviderConnectionView,
)
from kokoro_link.bootstrap.container import ServiceContainer
from kokoro_link.domain.entities.operator_profile import OperatorProfile
from kokoro_link.infrastructure.provider_settings.catalog import (
    ProviderCatalogEntry,
    ProviderFieldSpec,
    catalog_by_id,
)
from kokoro_link.infrastructure.provider_settings.model_discovery import (
    discover_models,
)
from kokoro_link.infrastructure.provider_settings.runtime_sync import (
    sync_provider_connections,
)

router = APIRouter(prefix="/admin/providers", tags=["admin-providers"])


class ProviderFieldSpecResponse(BaseModel):
    key: str
    label: str
    kind: str
    required: bool
    required_for_capabilities: list[str] = Field(default_factory=list)
    placeholder: str
    secret: bool
    advanced: bool


class ProviderCatalogEntryResponse(BaseModel):
    id: str
    display_name: str
    capabilities: list[str]
    auth_fields: list[ProviderFieldSpecResponse]
    config_fields: list[ProviderFieldSpecResponse]
    model_catalog_mode: str
    default_models: list[str]
    adapter_kind: str
    docs_url: str


class ProviderSecretStateResponse(BaseModel):
    configured: bool
    fingerprint: str = ""


class ProviderConnectionResponse(BaseModel):
    id: str
    provider: str
    label: str
    enabled: bool
    capabilities: list[str]
    config: dict[str, Any]
    secret: ProviderSecretStateResponse
    last_validated_at: datetime | None
    last_validation_error: str | None
    created_at: datetime | None
    updated_at: datetime | None


class ProviderConnectionCreateRequest(BaseModel):
    provider: str
    label: str = ""
    enabled: bool = True
    capabilities: list[str] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)
    secret: dict[str, Any] = Field(default_factory=dict)


class ProviderConnectionUpdateRequest(BaseModel):
    provider: str | None = None
    label: str | None = None
    enabled: bool | None = None
    capabilities: list[str] | None = None
    config: dict[str, Any] | None = None
    secret: dict[str, Any] | None = None
    clear_secret: bool = False


class ProviderConnectionTestResponse(BaseModel):
    ok: bool
    last_validated_at: datetime | None
    last_validation_error: str | None


class ListModelsRequest(BaseModel):
    provider: str
    capability: str
    config: dict[str, Any] = Field(default_factory=dict)
    secret: dict[str, Any] = Field(default_factory=dict)
    connection_id: str | None = None
    """If set, pull the stored secret instead of the draft ``secret`` —
    so the user can refresh the model list on an existing connection
    without re-typing the API key. The decrypted key is never returned
    to the client."""


class ListModelsResponse(BaseModel):
    models: list[str] = Field(default_factory=list)
    error: str | None = None


def _service(container: ServiceContainer) -> ProviderConnectionService:
    service = getattr(container, "provider_connection_service", None)
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="provider settings service not configured",
        )
    return service


def _require_provider_settings_unlocked(
    container: ServiceContainer = Depends(get_container),
) -> None:
    if is_cloud_mode(container):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="provider settings are disabled in cloud mode",
        )


@router.get("/catalog", response_model=list[ProviderCatalogEntryResponse])
async def get_catalog(
    admin: OperatorProfile = Depends(require_admin),
    container: ServiceContainer = Depends(get_container),
    _unlocked: None = Depends(_require_provider_settings_unlocked),
) -> list[ProviderCatalogEntryResponse]:
    del admin
    return [_catalog_entry(entry) for entry in _service(container).catalog()]


@router.get("", response_model=list[ProviderConnectionResponse])
async def list_connections(
    admin: OperatorProfile = Depends(require_admin),
    container: ServiceContainer = Depends(get_container),
    _unlocked: None = Depends(_require_provider_settings_unlocked),
) -> list[ProviderConnectionResponse]:
    del admin
    rows = await _service(container).list_connections()
    return [_connection(row) for row in rows]


@router.post(
    "",
    response_model=ProviderConnectionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_connection(
    payload: ProviderConnectionCreateRequest,
    admin: OperatorProfile = Depends(require_admin),
    container: ServiceContainer = Depends(get_container),
    _unlocked: None = Depends(_require_provider_settings_unlocked),
) -> ProviderConnectionResponse:
    del admin
    try:
        row = await _service(container).create_connection(
            provider=payload.provider,
            label=payload.label,
            enabled=payload.enabled,
            capabilities=payload.capabilities,
            config=payload.config,
            secret=payload.secret,
        )
    except ProviderConnectionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await sync_provider_connections(container)
    return _connection(row)


@router.post("/list-models", response_model=ListModelsResponse)
async def list_provider_models(
    payload: ListModelsRequest,
    admin: OperatorProfile = Depends(require_admin),
    container: ServiceContainer = Depends(get_container),
    _unlocked: None = Depends(_require_provider_settings_unlocked),
) -> ListModelsResponse:
    """Probe a provider's model catalogue for the BYOK admin UI.

    Body carries the draft connection so the admin can list models for
    settings that haven't been saved yet. ``connection_id`` (optional)
    lets the UI ask for "refresh" on an existing row, reusing the
    stored encrypted secret instead of asking the user to re-paste.
    """
    del admin
    catalog = catalog_by_id()
    entry = catalog.get(payload.provider)
    if entry is None:
        raise HTTPException(status_code=400, detail=f"unknown provider: {payload.provider}")

    base_url = str(payload.config.get("base_url") or "").strip()
    api_key = str(payload.secret.get("api_key") or "").strip()
    if not api_key and payload.connection_id:
        try:
            stored_secret = await _service(container).get_decrypted_secret(
                payload.connection_id,
            )
            api_key = str(stored_secret.get("api_key") or "").strip()
        except ProviderConnectionError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    result = await discover_models(
        provider_id=entry.id,
        adapter_kind=entry.adapter_kind,
        capability=payload.capability,
        base_url=base_url,
        api_key=api_key,
    )
    return ListModelsResponse(models=result.models, error=result.error)


@router.post("/test-draft", response_model=ProviderConnectionTestResponse)
async def test_draft_connection(
    payload: ProviderConnectionCreateRequest,
    admin: OperatorProfile = Depends(require_admin),
    container: ServiceContainer = Depends(get_container),
    _unlocked: None = Depends(_require_provider_settings_unlocked),
) -> ProviderConnectionTestResponse:
    del admin
    result = await _service(container).test_draft_connection(
        provider=payload.provider,
        enabled=payload.enabled,
        capabilities=payload.capabilities,
        config=payload.config,
        secret=payload.secret,
    )
    return _test_result(result)


@router.get("/{connection_id}", response_model=ProviderConnectionResponse)
async def get_connection(
    connection_id: str,
    admin: OperatorProfile = Depends(require_admin),
    container: ServiceContainer = Depends(get_container),
    _unlocked: None = Depends(_require_provider_settings_unlocked),
) -> ProviderConnectionResponse:
    del admin
    try:
        row = await _service(container).get_connection(connection_id)
    except ProviderConnectionError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _connection(row)


@router.patch("/{connection_id}", response_model=ProviderConnectionResponse)
async def update_connection(
    connection_id: str,
    payload: ProviderConnectionUpdateRequest,
    admin: OperatorProfile = Depends(require_admin),
    container: ServiceContainer = Depends(get_container),
    _unlocked: None = Depends(_require_provider_settings_unlocked),
) -> ProviderConnectionResponse:
    del admin
    try:
        row = await _service(container).update_connection(
            connection_id,
            provider=payload.provider,
            label=payload.label,
            enabled=payload.enabled,
            capabilities=payload.capabilities,
            config=payload.config,
            secret=payload.secret,
            clear_secret=payload.clear_secret,
        )
    except ProviderConnectionError as exc:
        status_code = 404 if "not found" in str(exc) else 400
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    await sync_provider_connections(container)
    return _connection(row)


@router.delete("/{connection_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_connection(
    connection_id: str,
    admin: OperatorProfile = Depends(require_admin),
    container: ServiceContainer = Depends(get_container),
    _unlocked: None = Depends(_require_provider_settings_unlocked),
) -> None:
    del admin
    await _service(container).delete_connection(connection_id)
    await sync_provider_connections(container)


@router.post("/{connection_id}/test", response_model=ProviderConnectionResponse)
async def test_connection(
    connection_id: str,
    admin: OperatorProfile = Depends(require_admin),
    container: ServiceContainer = Depends(get_container),
    _unlocked: None = Depends(_require_provider_settings_unlocked),
) -> ProviderConnectionResponse:
    del admin
    try:
        row = await _service(container).test_connection(connection_id)
    except ProviderConnectionError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _connection(row)


def _field(field: ProviderFieldSpec) -> ProviderFieldSpecResponse:
    return ProviderFieldSpecResponse(
        key=field.key,
        label=field.label,
        kind=field.kind,
        required=field.required,
        required_for_capabilities=list(field.required_for_capabilities),
        placeholder=field.placeholder,
        secret=field.secret,
        advanced=field.advanced,
    )


def _catalog_entry(entry: ProviderCatalogEntry) -> ProviderCatalogEntryResponse:
    return ProviderCatalogEntryResponse(
        id=entry.id,
        display_name=entry.display_name,
        capabilities=list(entry.capabilities),
        auth_fields=[_field(field) for field in entry.auth_fields],
        config_fields=[_field(field) for field in entry.config_fields],
        model_catalog_mode=entry.model_catalog_mode,
        default_models=list(entry.default_models),
        adapter_kind=entry.adapter_kind,
        docs_url=entry.docs_url,
    )


def _connection(row: ProviderConnectionView) -> ProviderConnectionResponse:
    return ProviderConnectionResponse(
        id=row.id,
        provider=row.provider,
        label=row.label,
        enabled=row.enabled,
        capabilities=list(row.capabilities),
        config=row.config,
        secret=ProviderSecretStateResponse(
            configured=row.secret.configured,
            fingerprint=row.secret.fingerprint,
        ),
        last_validated_at=row.last_validated_at,
        last_validation_error=row.last_validation_error,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _test_result(row: ProviderConnectionTestResult) -> ProviderConnectionTestResponse:
    return ProviderConnectionTestResponse(
        ok=row.ok,
        last_validated_at=row.last_validated_at,
        last_validation_error=row.last_validation_error,
    )
