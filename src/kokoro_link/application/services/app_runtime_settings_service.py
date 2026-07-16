"""Read/write site-level runtime settings groups (CORE_ENV_TO_ADMIN_CONFIG track 2).

Thin application service over the generic ``app_runtime_settings`` KV
(:class:`RuntimeSettingsRepositoryPort`). Each group is one JSON blob
under ``site.<group>``; this service (de)serialises through the matching
pydantic schema so callers deal in typed configs and validation errors
surface as ``ValueError`` the admin route turns into a 400.

DB is the source of truth once a group is written; when a group's key is
absent the caller supplies an env-derived default (the fallback + seed
path the plan mandates).
"""

from __future__ import annotations

import json
import logging

from pydantic import BaseModel, ValidationError

from kokoro_link.contracts.runtime_settings import RuntimeSettingsRepositoryPort
from kokoro_link.infrastructure.app_runtime_settings.schemas import (
    APP_SETTINGS_GROUPS,
    key_for_group,
)

_LOGGER = logging.getLogger(__name__)


class AppRuntimeSettingsError(ValueError):
    """Raised for an unknown group or a schema validation failure."""


class AppRuntimeSettingsService:
    def __init__(self, repository: RuntimeSettingsRepositoryPort | None) -> None:
        self._repository = repository

    @staticmethod
    def _schema(group: str) -> type[BaseModel]:
        schema = APP_SETTINGS_GROUPS.get(group)
        if schema is None:
            raise AppRuntimeSettingsError(f"unknown settings group: {group!r}")
        return schema

    async def get(
        self, group: str, *, default: BaseModel | None = None,
    ) -> BaseModel:
        """Return the persisted config for ``group``.

        Falls back to ``default`` (env-derived) when the key is absent or
        the stored blob is unparseable — a corrupt row must never take the
        site down, only lose that one override until re-saved."""
        schema = self._schema(group)
        if self._repository is None:
            return default if default is not None else schema()
        raw = await self._repository.get(key_for_group(group))
        if raw is None:
            return default if default is not None else schema()
        try:
            return schema.model_validate_json(raw)
        except (ValidationError, ValueError, json.JSONDecodeError):
            _LOGGER.warning(
                "app_runtime_settings group %s has an invalid blob; "
                "falling back to default",
                group,
            )
            return default if default is not None else schema()

    async def set(self, group: str, payload: dict) -> BaseModel:
        """Validate + persist a group. Returns the validated config."""
        schema = self._schema(group)
        try:
            config = schema.model_validate(payload)
        except ValidationError as exc:
            raise AppRuntimeSettingsError(str(exc)) from exc
        if self._repository is not None:
            await self._repository.set(
                key_for_group(group), config.model_dump_json(),
            )
        return config

    async def seed_if_absent(self, group: str, config: BaseModel) -> bool:
        """First-boot seed: write ``config`` only when the key is empty.

        Returns True when a row was seeded. Gate is "DB empty → seed" per
        the unified env-compat rule; after seeding the DB is authoritative
        and env changes no longer overwrite it."""
        if self._repository is None:
            return False
        self._schema(group)  # validate group name
        existing = await self._repository.get(key_for_group(group))
        if existing is not None:
            return False
        await self._repository.set(
            key_for_group(group), config.model_dump_json(),
        )
        return True


__all__ = [
    "AppRuntimeSettingsError",
    "AppRuntimeSettingsService",
]
