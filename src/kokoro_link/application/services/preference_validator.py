"""Startup-time repair for the model-pick preferences.

The UI writes `active_model` / `feature_models` straight to the DB. If
the operator later removes a provider (env change, adapter disabled,
model unloaded from LM Studio…), those prefs keep pointing at things
that no longer exist — and the only way to recover today is editing the
row by hand or clicking through the picker again. Worse, services that
resolve via :class:`PreferenceBackedActiveLLMProvider` only fall back on
unknown *provider_id*; an unknown *model_id* slips through and the
provider explodes on first use.

This module runs once at lifespan startup and rewrites invalid entries:

- Unknown ``provider_id`` → reset entry to env default (and drop the
  stale ``model_id``).
- Unknown ``model_id`` (provider lists models and ours isn't in the
  list) → clear just ``model_id``, leave ``provider_id`` alone so the
  provider's own default kicks in.
- Providers that return an empty ``list_models()`` are treated as
  "doesn't enumerate" and their entries are left untouched.

Priority order (feature_models > active_model > env) is unchanged — we
only repair what was already broken.
"""

from __future__ import annotations

import logging
from typing import Any

from kokoro_link.application.services.feature_keys import (
    GLOBAL_FEATURE_KEYS,
    LLM_FEATURE_GROUP_KEYS,
)
from kokoro_link.application.services.routing_reasoning import (
    REASONING_ENTRY_KEY,
    parse_reasoning_override,
    reasoning_pref_value,
)
from kokoro_link.application.services.routing_vision import (
    VISION_ENTRY_KEY,
    parse_vision_override,
)
from kokoro_link.contracts.llm import ChatModelPort, ChatModelRegistryPort
from kokoro_link.contracts.repositories import PreferencesRepositoryPort


_LOGGER = logging.getLogger(__name__)
_ACTIVE_MODEL_KEY = "active_model"
_FEATURE_MODELS_KEY = "feature_models"
_FEATURE_MODEL_GROUPS_KEY = "feature_model_groups"
_FAKE_PROVIDER_ID = "fake"


class ModelPreferenceValidator:
    def __init__(
        self,
        *,
        registry: ChatModelRegistryPort,
        preferences: PreferencesRepositoryPort,
        default_provider_id: str,
    ) -> None:
        self._registry = registry
        self._preferences = preferences
        self._default_provider_id = default_provider_id

    async def repair(self) -> None:
        await self._repair_active_model()
        await self._repair_feature_models()
        await self._repair_feature_model_groups()

    # ---- active_model ------------------------------------------------

    async def _repair_active_model(self) -> None:
        raw = await self._preferences.get(_ACTIVE_MODEL_KEY)
        if not isinstance(raw, dict):
            return
        provider_id = _coerce_str(raw.get("provider_id"))
        model_id = _coerce_str(raw.get("model_id"))
        # Preserve any routing-level vision pin across a rewrite — like
        # reasoning on the mapping entries, it is route metadata the
        # repair must not silently drop. A vision-only active_model (no
        # provider/model) returns early below and is left untouched.
        vision = parse_vision_override(raw)
        if provider_id is None and model_id is None:
            return

        provider = self._resolve_or_none(provider_id)
        if provider is None:
            fallback_provider_id = self._fallback_provider_id()
            _LOGGER.warning(
                "active_model pref points at unknown provider %r; "
                "resetting to runtime fallback %r",
                provider_id, fallback_provider_id,
            )
            await self._preferences.set(
                _ACTIVE_MODEL_KEY,
                _active_model_value(fallback_provider_id, None, vision),
            )
            return

        if model_id is None:
            return
        if await _model_id_is_valid(provider, model_id):
            return
        _LOGGER.warning(
            "active_model pref points at unknown model %r on provider %r; "
            "clearing model_id (provider will use its own default)",
            model_id, provider_id,
        )
        await self._preferences.set(
            _ACTIVE_MODEL_KEY,
            _active_model_value(provider_id, None, vision),
        )

    # ---- feature_models ----------------------------------------------

    async def _repair_feature_models(self) -> None:
        raw = await self._preferences.get(_FEATURE_MODELS_KEY)
        if not isinstance(raw, dict):
            return

        repaired, changed = await self._repair_mapping(
            raw,
            allowed_keys=set(GLOBAL_FEATURE_KEYS),
            entry_label="feature_models",
        )
        if changed:
            await self._preferences.set(_FEATURE_MODELS_KEY, repaired)

    async def _repair_feature_model_groups(self) -> None:
        raw = await self._preferences.get(_FEATURE_MODEL_GROUPS_KEY)
        if not isinstance(raw, dict):
            return

        repaired, changed = await self._repair_mapping(
            raw,
            allowed_keys=set(LLM_FEATURE_GROUP_KEYS),
            entry_label="feature_model_groups",
        )
        if changed:
            await self._preferences.set(_FEATURE_MODEL_GROUPS_KEY, repaired)

    async def _repair_mapping(
        self,
        raw: dict[Any, Any],
        *,
        allowed_keys: set[str],
        entry_label: str,
    ) -> tuple[dict[str, dict[str, Any]], bool]:
        repaired: dict[str, dict[str, Any]] = {}
        changed = False
        for key, entry in raw.items():
            if not isinstance(key, str) or key not in allowed_keys:
                # Unknown key — drop it (matches the system route's
                # write-side filter so we don't keep stale renames around).
                changed = True
                continue
            if not isinstance(entry, dict):
                changed = True
                continue
            provider_id = _coerce_str(entry.get("provider_id"))
            model_id = _coerce_str(entry.get("model_id"))
            # Routing-level reasoning override rides on the same entry.
            # Re-normalise through the shared parser so malformed shapes
            # are dropped instead of carried forever; a well-formed
            # object round-trips byte-identical (no spurious rewrites).
            reasoning = reasoning_pref_value(parse_reasoning_override(entry))
            if (
                REASONING_ENTRY_KEY in entry
                and entry.get(REASONING_ENTRY_KEY) != reasoning
            ):
                _LOGGER.warning(
                    "%s[%s] carries a malformed reasoning override; "
                    "normalising",
                    entry_label, key,
                )
                changed = True
            # Routing-level vision pin rides on the same entry. Re-normalise
            # through the shared parser so malformed shapes (stringy /
            # numeric) are dropped, and a valid bool round-trips untouched.
            vision = parse_vision_override(entry)
            if (
                VISION_ENTRY_KEY in entry
                and entry.get(VISION_ENTRY_KEY) != vision
            ):
                _LOGGER.warning(
                    "%s[%s] carries a malformed supports_vision override; "
                    "normalising",
                    entry_label, key,
                )
                changed = True

            if (
                provider_id is None
                and model_id is None
                and reasoning is None
                and vision is None
            ):
                # All-null entry — same as "no override". A reasoning-only
                # or vision-only entry is real configuration and stays.
                changed = True
                continue

            if provider_id is not None:
                provider = self._resolve_or_none(provider_id)
                if provider is None:
                    _LOGGER.warning(
                        "%s[%s] points at unknown provider %r; "
                        "dropping override",
                        entry_label, key, provider_id,
                    )
                    changed = True
                    continue
                if model_id is not None and not await _model_id_is_valid(
                    provider, model_id,
                ):
                    _LOGGER.warning(
                        "%s[%s] points at unknown model %r on "
                        "provider %r; clearing model_id",
                        entry_label, key, model_id, provider_id,
                    )
                    model_id = None
                    changed = True

            repaired_entry: dict[str, Any] = {
                "provider_id": provider_id,
                "model_id": model_id,
            }
            if reasoning is not None:
                repaired_entry[REASONING_ENTRY_KEY] = reasoning
            if vision is not None:
                repaired_entry[VISION_ENTRY_KEY] = vision
            repaired[key] = repaired_entry

        return repaired, changed

    # ---- helpers -----------------------------------------------------

    def _resolve_or_none(self, provider_id: str | None) -> ChatModelPort | None:
        if provider_id is None:
            return None
        try:
            return self._registry.resolve(provider_id)
        except Exception:
            return None

    def _fallback_provider_id(self) -> str:
        ids = self._registry.list_ids()
        if self._default_provider_id in ids:
            return self._default_provider_id
        for provider_id in ids:
            if provider_id != _FAKE_PROVIDER_ID:
                return provider_id
        if _FAKE_PROVIDER_ID in ids:
            return _FAKE_PROVIDER_ID
        return self._default_provider_id


async def _model_id_is_valid(provider: ChatModelPort, model_id: str) -> bool:
    """``True`` iff the provider enumerates models and ours is in the list.

    Providers that return an empty list ("don't enumerate") get a free
    pass — we can't tell whether the id is valid, so we leave it alone
    rather than wiping operator intent.
    """
    try:
        models = await provider.list_models()
    except Exception:
        _LOGGER.exception(
            "provider %r raised while listing models; skipping validation",
            provider.provider_id,
        )
        return True
    if not models:
        return True
    return model_id in models


def _coerce_str(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    return None


def _active_model_value(
    provider_id: str | None,
    model_id: str | None,
    vision: bool | None,
) -> dict[str, Any]:
    """Rebuild an ``active_model`` value, carrying the vision pin only
    when one was set so unrelated rewrites stay byte-identical to before
    this feature (existing repair tests assert exact 2-key dicts)."""
    value: dict[str, Any] = {"provider_id": provider_id, "model_id": model_id}
    if vision is not None:
        value[VISION_ENTRY_KEY] = vision
    return value
