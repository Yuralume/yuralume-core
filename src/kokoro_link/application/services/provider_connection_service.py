"""Application service for BYOK provider connections."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from kokoro_link.contracts.provider_settings import (
    ProviderConnection,
    ProviderConnectionRepositoryPort,
    ProviderSecretState,
)
from kokoro_link.infrastructure.provider_settings.catalog import (
    ProviderCatalogEntry,
    catalog_by_id,
    list_provider_catalog,
)
from kokoro_link.infrastructure.security.provider_secret_cipher import (
    ProviderSecretCipher,
    ProviderSecretCipherError,
)


class ProviderConnectionError(ValueError):
    """Provider settings validation error."""


@dataclass(frozen=True, slots=True)
class ProviderConnectionView:
    id: str
    provider: str
    label: str
    enabled: bool
    capabilities: tuple[str, ...]
    config: dict[str, Any]
    secret: ProviderSecretState
    last_validated_at: datetime | None
    last_validation_error: str | None
    created_at: datetime | None
    updated_at: datetime | None


@dataclass(frozen=True, slots=True)
class ProviderConnectionTestResult:
    ok: bool
    last_validated_at: datetime | None
    last_validation_error: str | None


class ProviderConnectionService:
    def __init__(
        self,
        *,
        repository: ProviderConnectionRepositoryPort,
        cipher: ProviderSecretCipher,
    ) -> None:
        self._repository = repository
        self._cipher = cipher
        self._catalog = catalog_by_id()

    def catalog(self) -> tuple[ProviderCatalogEntry, ...]:
        return list_provider_catalog()

    async def list_connections(self) -> list[ProviderConnectionView]:
        rows = await self._repository.list_all()
        return [self._redact(row) for row in rows]

    async def list_enabled_runtime(
        self,
        capability: str | None = None,
    ) -> list[ProviderConnection]:
        return await self._repository.list_enabled(capability=capability)

    async def get_connection(self, connection_id: str) -> ProviderConnectionView:
        row = await self._get_required(connection_id)
        return self._redact(row)

    async def get_decrypted_secret(self, connection_id: str) -> dict[str, Any]:
        row = await self._get_required(connection_id)
        if not row.encrypted_secret:
            return {}
        return self._cipher.decrypt(row.encrypted_secret)

    async def create_connection(
        self,
        *,
        provider: str,
        label: str,
        enabled: bool,
        capabilities: list[str],
        config: dict[str, Any] | None = None,
        secret: dict[str, Any] | None = None,
    ) -> ProviderConnectionView:
        entry = self._require_catalog(provider)
        cleaned_capabilities = self._clean_capabilities(entry, capabilities)
        cleaned_config = self._clean_config(
            entry,
            config or {},
            fields=entry.config_fields,
            payload_name="config",
        )
        cleaned_secret = self._clean_config(
            entry,
            secret or {},
            fields=entry.auth_fields,
            payload_name="secret",
        )
        self._validate_required(
            entry,
            config=cleaned_config,
            secret=cleaned_secret,
            has_existing_secret=False,
            enabled=enabled,
            capabilities=cleaned_capabilities,
        )
        encrypted_secret, fingerprint = self._encrypt_secret(cleaned_secret)
        now = datetime.now(timezone.utc)
        row = ProviderConnection(
            id=str(uuid.uuid4()),
            provider=entry.id,
            label=self._clean_label(label, entry),
            enabled=bool(enabled),
            capabilities=tuple(cleaned_capabilities),
            config=cleaned_config,
            encrypted_secret=encrypted_secret,
            secret_fingerprint=fingerprint,
            created_at=now,
            updated_at=now,
        )
        saved = await self._repository.save(row)
        return self._redact(saved)

    async def update_connection(
        self,
        connection_id: str,
        *,
        provider: str | None = None,
        label: str | None = None,
        enabled: bool | None = None,
        capabilities: list[str] | None = None,
        config: dict[str, Any] | None = None,
        secret: dict[str, Any] | None = None,
        clear_secret: bool = False,
    ) -> ProviderConnectionView:
        current = await self._get_required(connection_id)
        entry = self._require_catalog(provider or current.provider)
        cleaned_capabilities = (
            tuple(self._clean_capabilities(entry, capabilities))
            if capabilities is not None
            else current.capabilities
        )
        cleaned_config = (
            self._clean_config(
                entry,
                config,
                fields=entry.config_fields,
                payload_name="config",
            )
            if config is not None
            else dict(current.config)
        )
        encrypted_secret = current.encrypted_secret
        fingerprint = current.secret_fingerprint
        secret_for_validation: dict[str, Any] = {}
        if clear_secret:
            encrypted_secret = ""
            fingerprint = ""
        elif secret is not None:
            meaningful = self._clean_config(
                entry,
                secret,
                fields=entry.auth_fields,
                payload_name="secret",
            )
            secret_for_validation = meaningful
            if meaningful:
                encrypted_secret, fingerprint = self._encrypt_secret(meaningful)
        elif encrypted_secret:
            secret_for_validation = self._cipher.decrypt(encrypted_secret)
        self._validate_required(
            entry,
            config=cleaned_config,
            secret=secret_for_validation,
            has_existing_secret=bool(encrypted_secret),
            enabled=current.enabled if enabled is None else bool(enabled),
            capabilities=cleaned_capabilities,
        )
        row = ProviderConnection(
            id=current.id,
            provider=entry.id,
            label=self._clean_label(label if label is not None else current.label, entry),
            enabled=current.enabled if enabled is None else bool(enabled),
            capabilities=cleaned_capabilities,
            config=cleaned_config,
            encrypted_secret=encrypted_secret,
            secret_fingerprint=fingerprint,
            last_validated_at=current.last_validated_at,
            last_validation_error=current.last_validation_error,
            created_at=current.created_at,
            updated_at=datetime.now(timezone.utc),
        )
        saved = await self._repository.save(row)
        return self._redact(saved)

    async def delete_connection(self, connection_id: str) -> None:
        await self._repository.delete(connection_id)

    async def record_runtime_status(
        self,
        connection_id: str,
        *,
        error: str | None,
    ) -> None:
        """Persist the outcome of the last runtime sync attempt.

        ``runtime_sync`` calls this after building (or failing to build)
        the adapter for a row, so the admin UI can surface the same
        diagnostic that previously only existed in backend logs. ``error``
        is sanitised here in the same way as test results.
        """
        row = await self._repository.get(connection_id)
        if row is None:
            return
        sanitized = _sanitize_error(error) if error else None
        now = datetime.now(timezone.utc)
        # Avoid writing identical state on every sync — sync runs on every
        # BYOK CRUD plus on app startup, so we'd otherwise churn updated_at
        # for no reason. Skip when the row is already in the target state.
        if sanitized == row.last_validation_error:
            if sanitized is None and row.last_validated_at is not None:
                return
            if sanitized is not None:
                return
        updated = ProviderConnection(
            id=row.id,
            provider=row.provider,
            label=row.label,
            enabled=row.enabled,
            capabilities=row.capabilities,
            config=row.config,
            encrypted_secret=row.encrypted_secret,
            secret_fingerprint=row.secret_fingerprint,
            last_validated_at=None if sanitized else now,
            last_validation_error=sanitized,
            created_at=row.created_at,
            updated_at=now,
        )
        await self._repository.save(updated)

    async def test_connection(
        self,
        connection_id: str,
    ) -> ProviderConnectionView:
        row = await self._get_required(connection_id)
        error = None
        try:
            entry = self._require_catalog(row.provider)
            self._clean_capabilities(entry, list(row.capabilities))
            secret = self._cipher.decrypt(row.encrypted_secret) if row.encrypted_secret else {}
            self._validate_required(
                entry,
                config=dict(row.config),
                secret=secret,
                has_existing_secret=bool(row.encrypted_secret),
                enabled=row.enabled,
                capabilities=row.capabilities,
            )
        except Exception as exc:
            error = _sanitize_error(str(exc))
        updated = ProviderConnection(
            id=row.id,
            provider=row.provider,
            label=row.label,
            enabled=row.enabled,
            capabilities=row.capabilities,
            config=row.config,
            encrypted_secret=row.encrypted_secret,
            secret_fingerprint=row.secret_fingerprint,
            last_validated_at=None if error else datetime.now(timezone.utc),
            last_validation_error=error,
            created_at=row.created_at,
            updated_at=datetime.now(timezone.utc),
        )
        saved = await self._repository.save(updated)
        return self._redact(saved)

    async def test_draft_connection(
        self,
        *,
        provider: str,
        enabled: bool,
        capabilities: list[str],
        config: dict[str, Any] | None = None,
        secret: dict[str, Any] | None = None,
    ) -> ProviderConnectionTestResult:
        error = None
        try:
            entry = self._require_catalog(provider)
            self._clean_capabilities(entry, capabilities)
            cleaned_config = self._clean_config(
                entry,
                config or {},
                fields=entry.config_fields,
                payload_name="config",
            )
            cleaned_secret = self._clean_config(
                entry,
                secret or {},
                fields=entry.auth_fields,
                payload_name="secret",
            )
            self._validate_required(
                entry,
                config=cleaned_config,
                secret=cleaned_secret,
                has_existing_secret=bool(cleaned_secret),
                enabled=enabled,
                capabilities=capabilities,
            )
        except Exception as exc:
            error = _sanitize_error(str(exc))
        return ProviderConnectionTestResult(
            ok=error is None,
            last_validated_at=None if error else datetime.now(timezone.utc),
            last_validation_error=error,
        )

    async def _get_required(self, connection_id: str) -> ProviderConnection:
        row = await self._repository.get(connection_id)
        if row is None:
            raise ProviderConnectionError("provider connection not found")
        return row

    def _require_catalog(self, provider: str) -> ProviderCatalogEntry:
        entry = self._catalog.get(provider)
        if entry is None:
            raise ProviderConnectionError(f"unknown provider: {provider}")
        return entry

    def _clean_capabilities(
        self,
        entry: ProviderCatalogEntry,
        capabilities: list[str],
    ) -> list[str]:
        allowed = set(entry.capabilities)
        cleaned: list[str] = []
        for capability in capabilities:
            normalized = str(capability).strip().lower()
            if not normalized:
                continue
            if normalized not in allowed:
                raise ProviderConnectionError(
                    f"{entry.id} does not support capability: {normalized}",
                )
            if normalized not in cleaned:
                cleaned.append(normalized)
        if not cleaned:
            cleaned = [entry.capabilities[0]]
        return cleaned

    def _clean_config(
        self,
        entry: ProviderCatalogEntry,
        config: dict[str, Any],
        *,
        fields: tuple[Any, ...],
        payload_name: str,
    ) -> dict[str, Any]:
        allowed = {field.key for field in fields}
        cleaned: dict[str, Any] = {}
        for key, value in config.items():
            if not isinstance(key, str):
                continue
            normalized_key = key.strip()
            if not normalized_key:
                continue
            if normalized_key not in allowed:
                raise ProviderConnectionError(
                    f"{entry.id} {payload_name} does not support field: {normalized_key}",
                )
            if isinstance(value, str):
                value = value.strip()
            if value in ("", None):
                continue
            cleaned[normalized_key] = value
        return cleaned

    def _validate_required(
        self,
        entry: ProviderCatalogEntry,
        *,
        config: dict[str, Any],
        secret: dict[str, Any],
        has_existing_secret: bool,
        enabled: bool,
        capabilities: tuple[str, ...] | list[str] | None = None,
    ) -> None:
        if not enabled:
            return
        selected_caps = set(capabilities or ())
        for field in entry.config_fields:
            if field.required and field.key not in config:
                raise ProviderConnectionError(
                    f"{entry.id} config requires field: {field.key}",
                )
            # Per-capability required: catalog marks fields (default_model,
            # embedding_model, …) that are mandatory only when the matching
            # capability is selected. Lets one provider definition serve all
            # combinations without forcing irrelevant fields on the user.
            if field.required_for_capabilities and field.key not in config:
                triggered = selected_caps.intersection(field.required_for_capabilities)
                if triggered:
                    raise ProviderConnectionError(
                        f"{entry.id} config requires field {field.key!r} "
                        f"when capability {sorted(triggered)[0]!r} is selected",
                    )
        for field in entry.auth_fields:
            if not field.required:
                continue
            if field.key in secret:
                continue
            if has_existing_secret:
                continue
            raise ProviderConnectionError(
                f"{entry.id} secret requires field: {field.key}",
            )

    def _clean_label(self, label: str, entry: ProviderCatalogEntry) -> str:
        value = str(label or "").strip()
        return value or entry.display_name

    def _encrypt_secret(self, secret: dict[str, Any]) -> tuple[str, str]:
        if not secret:
            return "", ""
        try:
            return self._cipher.encrypt(secret), self._cipher.fingerprint(secret)
        except ProviderSecretCipherError as exc:
            raise ProviderConnectionError(str(exc)) from exc

    def _redact(self, row: ProviderConnection) -> ProviderConnectionView:
        return ProviderConnectionView(
            id=row.id,
            provider=row.provider,
            label=row.label,
            enabled=row.enabled,
            capabilities=row.capabilities,
            config=dict(row.config),
            secret=ProviderSecretState(
                configured=bool(row.encrypted_secret),
                fingerprint=row.secret_fingerprint,
            ),
            last_validated_at=row.last_validated_at,
            last_validation_error=row.last_validation_error,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


_SECRET_PATTERN = re.compile(r"(sk|key|token|secret|bearer)[-_A-Za-z0-9]{8,}")


def _sanitize_error(message: str) -> str:
    return _SECRET_PATTERN.sub("[redacted]", message)[:500]
