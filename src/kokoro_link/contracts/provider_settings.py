"""Installation-level BYOK provider settings contracts.

Provider keys are not runtime knobs and must not live in the generic
``app_runtime_settings`` KV table. This module defines a typed boundary
for encrypted provider connections that can back LLM / image / video /
TTS adapters without leaking secrets to API callers.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Protocol


ProviderCapability = str


@dataclass(frozen=True, slots=True)
class ProviderConnection:
    id: str
    provider: str
    label: str
    enabled: bool
    capabilities: tuple[ProviderCapability, ...] = field(default_factory=tuple)
    config: dict[str, object] = field(default_factory=dict)
    encrypted_secret: str = ""
    secret_fingerprint: str = ""
    last_validated_at: datetime | None = None
    last_validation_error: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def with_timestamps(
        self,
        *,
        created_at: datetime | None = None,
        updated_at: datetime | None = None,
    ) -> "ProviderConnection":
        return replace(
            self,
            created_at=created_at if created_at is not None else self.created_at,
            updated_at=updated_at if updated_at is not None else self.updated_at,
        )


@dataclass(frozen=True, slots=True)
class ProviderSecretState:
    configured: bool
    fingerprint: str = ""


class ProviderConnectionRepositoryPort(Protocol):
    async def list_all(self) -> list[ProviderConnection]:
        """Return all non-deleted provider connections."""

    async def list_enabled(
        self,
        *,
        capability: ProviderCapability | None = None,
    ) -> list[ProviderConnection]:
        """Return enabled connections, optionally filtered by capability."""

    async def get(self, connection_id: str) -> ProviderConnection | None:
        """Return one connection by id, or ``None`` when missing."""

    async def save(self, connection: ProviderConnection) -> ProviderConnection:
        """Insert or update a provider connection."""

    async def delete(self, connection_id: str) -> None:
        """Soft-delete or remove one provider connection."""
