"""Preference-backed ``ActiveLLMProviderPort`` implementation.

Three layers of preference drive resolution, highest priority first:

- Per-character ``character.feature_models[key]`` — only consulted
  when the caller passes a ``character`` to the resolver. Lets
  operators pin one character to a different LLM than the rest of
  the app (e.g. character A on Anthropic Sonnet while the global
  picker stays on LM Studio).

- Global ``feature_models`` — per-feature overrides written by the
  advanced per-feature picker. Shape::

      {
        "post_turn": {"provider_id": "anthropic", "model_id": "..."},
        "goal_review": {"provider_id": "lmstudio", "model_id": "..."},
        ...
      }

- Global ``active_model`` — provider/model pick written by the
  frontend's primary model picker. Shape::

      {"provider_id": "anthropic", "model_id": "claude-sonnet-4-5"}

Final fallback is the container's default provider id. Unknown
``feature_key`` or malformed entries degrade silently to the next
level — auxiliary services are non-critical path, they should not
crash the turn.
"""

from __future__ import annotations

import logging
from typing import Any

from kokoro_link.contracts.active_llm import ActiveLLMProviderPort
from kokoro_link.contracts.llm import (
    ChatModelPort,
    ChatModelRegistryPort,
    ReasoningOverrides,
)
from kokoro_link.contracts.repositories import PreferencesRepositoryPort
from kokoro_link.domain.entities.character import Character, FeatureModelOverride
from kokoro_link.application.services.feature_keys import FEATURE_TO_GROUP
from kokoro_link.application.services.nsfw_mode import NsfwModeService
from kokoro_link.application.services.routing_reasoning import (
    parse_reasoning_override,
)
from kokoro_link.application.services.routing_vision import (
    parse_vision_override,
)
from kokoro_link.application.services.scoped_preferences import (
    get_preference_with_user_fallback,
)
from kokoro_link.domain.value_objects.content_flow import (
    CONTENT_TOLERANCE_COMMUNITY,
    normalize_content_tolerance,
)


_LOGGER = logging.getLogger(__name__)
_ACTIVE_MODEL_KEY = "active_model"
_FEATURE_MODELS_KEY = "feature_models"
_FEATURE_MODEL_GROUPS_KEY = "feature_model_groups"
"""Both must match the API route constants."""

_FAKE_PROVIDER_ID = "fake"

ROUTING_SOURCE_CHARACTER_FEATURE = "character_feature"
ROUTING_SOURCE_GLOBAL_FEATURE = "global_feature"
ROUTING_SOURCE_GLOBAL_GROUP = "global_group"
ROUTING_SOURCE_ACTIVE_MODEL = "active_model"
ROUTING_SOURCE_RUNTIME_FALLBACK = "runtime_fallback"
ROUTING_SOURCE_NSFW_MODE = "nsfw_mode"
ROUTING_SOURCE_NSFW_CONTENT = "nsfw_content"


class PreferenceBackedActiveLLMProvider(ActiveLLMProviderPort):
    def __init__(
        self,
        *,
        registry: ChatModelRegistryPort,
        preferences: PreferencesRepositoryPort,
        default_provider_id: str,
        nsfw_mode_service: NsfwModeService | None = None,
    ) -> None:
        self._registry = registry
        self._preferences = preferences
        self._default_provider_id = default_provider_id
        self._nsfw_mode_service = nsfw_mode_service

    async def resolve(
        self,
        feature_key: str | None = None,
        *,
        character: Character | None = None,
        operator_id: str | None = None,
        content_tolerance: str | None = None,
    ) -> ChatModelPort:
        _ = operator_id
        provider_id = await self._read_preferred_provider_id(
            feature_key,
            character=character,
            content_tolerance=content_tolerance,
        )
        if provider_id is None:
            provider_id = self._fallback_provider_id()
        try:
            model = self._registry.resolve(provider_id)
        except Exception:
            fallback_provider_id = self._fallback_provider_id()
            _LOGGER.warning(
                "active LLM resolve: provider %r not registered "
                "(feature=%s, character=%s); falling back to default %r",
                provider_id, feature_key,
                character.id if character is not None else None,
                fallback_provider_id,
            )
            model = self._registry.resolve(fallback_provider_id)
        model = await self._maybe_bind_reasoning(
            model,
            feature_key,
            character=character,
            content_tolerance=content_tolerance,
        )
        return await self._maybe_bind_vision(
            model,
            feature_key,
            character=character,
            content_tolerance=content_tolerance,
        )

    async def resolve_model_id(
        self,
        feature_key: str | None = None,
        *,
        character: Character | None = None,
        operator_id: str | None = None,
        content_tolerance: str | None = None,
    ) -> str | None:
        _ = operator_id
        nsfw_target = await self._read_nsfw_target(character=character)
        if nsfw_target is not None:
            self._require_registered_nsfw_provider(nsfw_target.llm_provider_id)
            return nsfw_target.llm_model_id
        if _requires_community_tolerance(content_tolerance):
            nsfw_target = await self._read_configured_nsfw_target(
                character=character,
            )
            if nsfw_target is None:
                raise RuntimeError(
                    "nsfw content requires configured community LLM target",
                )
            self._require_registered_nsfw_provider(nsfw_target.llm_provider_id)
            return nsfw_target.llm_model_id
        # Per-character override first — may carry its own model_id
        # even if provider_id is blank (blank provider falls through to
        # the global pick but the explicit model still applies).
        if feature_key and character is not None:
            override = character.feature_model_for(feature_key)
            if override is not None and override.model_id:
                if override.provider_id and not self._provider_is_registered(
                    override.provider_id,
                ):
                    _LOGGER.warning(
                        "active LLM model resolve: character override "
                        "provider %r is not registered (feature=%s, "
                        "character=%s); ignoring its model_id %r",
                        override.provider_id, feature_key, character.id,
                        override.model_id,
                    )
                else:
                    return override.model_id
        # Global per-feature override next.
        if feature_key:
            feature_entry = await self._read_feature_entry(
                feature_key, character=character,
            )
            if feature_entry is not None:
                value = feature_entry.get("model_id")
                if isinstance(value, str) and value.strip():
                    provider_value = feature_entry.get("provider_id")
                    if (
                        isinstance(provider_value, str)
                        and provider_value.strip()
                        and not self._provider_is_registered(provider_value)
                    ):
                        _LOGGER.warning(
                            "active LLM model resolve: feature override "
                            "provider %r is not registered (feature=%s); "
                            "ignoring its model_id %r",
                            provider_value, feature_key, value,
                        )
                    else:
                        return value
                # Per-feature entry present but no model_id → fall
                # through to global so a blank override means "inherit
                # provider-default", not "override with None".
            group_entry = await self._read_group_entry(
                feature_key,
                character=character,
            )
            if group_entry is not None:
                value = group_entry.get("model_id")
                if isinstance(value, str) and value.strip():
                    provider_value = group_entry.get("provider_id")
                    if (
                        isinstance(provider_value, str)
                        and provider_value.strip()
                        and not self._provider_is_registered(provider_value)
                    ):
                        _LOGGER.warning(
                            "active LLM model resolve: group override "
                            "provider %r is not registered (feature=%s); "
                            "ignoring its model_id %r",
                            provider_value, feature_key, value,
                        )
                    else:
                        return value
                # Group entry present but no model_id → fall through
                # to active_model, mirroring feature override semantics.
        raw = await self._read_pref(_ACTIVE_MODEL_KEY, character=character)
        if not isinstance(raw, dict):
            return None
        value = raw.get("model_id")
        if isinstance(value, str) and value.strip():
            provider_value = raw.get("provider_id")
            if (
                isinstance(provider_value, str)
                and provider_value.strip()
                and not self._provider_is_registered(provider_value)
            ):
                _LOGGER.warning(
                    "active LLM model resolve: active_model provider %r "
                    "is not registered; ignoring its model_id %r",
                    provider_value, value,
                )
                return None
            return value
        return None

    async def is_fake(
        self,
        feature_key: str | None = None,
        *,
        character: Character | None = None,
        operator_id: str | None = None,
        content_tolerance: str | None = None,
    ) -> bool:
        _ = operator_id
        provider_id = await self._read_preferred_provider_id(
            feature_key,
            character=character,
            content_tolerance=content_tolerance,
        )
        if provider_id is None:
            provider_id = self._fallback_provider_id()
        return provider_id == _FAKE_PROVIDER_ID

    async def resolve_routing_source(
        self,
        feature_key: str | None = None,
        *,
        character: Character | None = None,
        content_tolerance: str | None = None,
    ) -> str:
        """Return the preference layer that supplies the provider.

        This is a lightweight observability hook: it mirrors
        :meth:`resolve` provider precedence without returning the model
        object. Unknown feature keys deliberately skip group routing.
        """
        source, provider_id = await self._read_preferred_provider(
            feature_key,
            character=character,
            content_tolerance=content_tolerance,
        )
        if provider_id is None or not self._provider_is_registered(provider_id):
            return ROUTING_SOURCE_RUNTIME_FALLBACK
        return source or ROUTING_SOURCE_RUNTIME_FALLBACK

    # ---- internals ----------------------------------------------------

    async def _maybe_bind_reasoning(
        self,
        model: ChatModelPort,
        feature_key: str | None,
        *,
        character: Character | None,
        content_tolerance: str | None,
    ) -> ChatModelPort:
        """Bind a routing-level reasoning override onto the resolved
        adapter, when one applies.

        Follows the same feature → group precedence as model routing but
        falls through independently, so an entry can pin reasoning
        without pinning a model (and vice versa). NSFW reroutes skip the
        override — the posture was written for the normal route, not
        the dedicated NSFW target. Adapters without
        ``with_reasoning_overrides`` (fake, cloud gateway) pass through
        unchanged, as does everything when no override is set: the
        registry singleton is returned as-is.
        """
        if not feature_key:
            return model
        binder = getattr(model, "with_reasoning_overrides", None)
        if binder is None:
            return model
        if await self._read_nsfw_target(character=character) is not None:
            return model
        if _requires_community_tolerance(content_tolerance):
            return model
        overrides = await self._read_reasoning_overrides(
            feature_key, character=character,
        )
        if overrides is None:
            return model
        try:
            return binder(overrides)
        except Exception:
            _LOGGER.exception(
                "active LLM resolve: reasoning override binding failed "
                "(feature=%s); using connection defaults",
                feature_key,
            )
            return model

    async def _read_reasoning_overrides(
        self,
        feature_key: str,
        *,
        character: Character | None,
    ) -> ReasoningOverrides | None:
        feature_entry = await self._read_feature_entry(
            feature_key, character=character,
        )
        overrides = parse_reasoning_override(feature_entry)
        if overrides is not None:
            return overrides
        group_entry = await self._read_group_entry(
            feature_key, character=character,
        )
        return parse_reasoning_override(group_entry)

    async def _maybe_bind_vision(
        self,
        model: ChatModelPort,
        feature_key: str | None,
        *,
        character: Character | None,
        content_tolerance: str | None,
    ) -> ChatModelPort:
        """Bind a routing-level vision override onto the resolved adapter.

        Unlike reasoning, a vision override applies even to plain
        ``active_model`` resolution (no ``feature_key``): an aggregator
        connection fronts both vision and text-only models, so the
        connection flag can't be right for every route and the primary
        pick must be able to correct it too. NSFW reroutes skip the
        override — the entry describes the normal route's model, not the
        hijacked NSFW target. Adapters without ``with_supports_vision``
        (fake, cloud gateway) pass through unchanged, as does everything
        when no override is pinned: the (already reasoning-bound or
        registry) instance is returned as-is.
        """
        binder = getattr(model, "with_supports_vision", None)
        if binder is None:
            return model
        if await self._read_nsfw_target(character=character) is not None:
            return model
        if _requires_community_tolerance(content_tolerance):
            return model
        override = await self._read_vision_override(
            feature_key, character=character,
        )
        if override is None:
            return model
        try:
            return binder(override)
        except Exception:
            _LOGGER.exception(
                "active LLM resolve: vision override binding failed "
                "(feature=%s); using connection default",
                feature_key,
            )
            return model

    async def _read_vision_override(
        self,
        feature_key: str | None,
        *,
        character: Character | None,
    ) -> bool | None:
        """Resolve the vision pin with character → feature → group →
        active_model precedence. The first layer with a non-None
        override wins.

        A layer's flag must ride with the layer that actually supplied
        the MODEL, so a lower layer's flag is eligible only when no
        higher layer pinned any part of the model (provider_id or
        model_id):

        * per-character ``FeatureModelOverride`` — the HIGHEST
          model-supplying layer. It carries no vision field by design
          (per-character rows inherit the connection flag), so when it
          pins a provider/model ALL lower-layer flags are suppressed —
          they describe different models. A character entry pinning
          nothing does not block (kept symmetric with the metadata-only
          exemption below).
        * feature entry flag — eligible when the character layer did
          not pin.
        * group entry flag — eligible only when the feature entry also
          does not pin a provider/model. Even when the feature pins only
          model_id (provider falls through), the capability-determining
          model came from the feature layer, so the group's flag —
          written for the group's own pick — must not ride onto it.
        * active_model flag — eligible only when no layer above pinned
          a provider/model. Otherwise an active_model
          ``supports_vision: true`` (set for e.g. an OpenAI pick) would
          bleed onto a group-pinned text-only aggregator model and
          re-attach images to it.

        A metadata-only entry (reasoning / vision, no provider/model
        pin) does NOT block lower layers — the model came from the next
        layer down. With no ``feature_key`` (plain resolve) the
        active_model flag applies, unchanged."""
        if feature_key and character is not None:
            char_override = _per_character_override(character, feature_key)
            if char_override is not None and (
                char_override.provider_id or char_override.model_id
            ):
                # The character layer supplied the model; it has no
                # vision field → inherit the connection flag and keep
                # every lower-layer flag out.
                return None
        if feature_key:
            feature_entry = await self._read_feature_entry(
                feature_key, character=character,
            )
            override = parse_vision_override(feature_entry)
            if override is not None:
                return override
            if _entry_pins_model(feature_entry):
                # The feature layer supplied the model without asserting
                # vision → inherit the connection flag; lower layers'
                # flags describe different models.
                return None
            group_entry = await self._read_group_entry(
                feature_key, character=character,
            )
            override = parse_vision_override(group_entry)
            if override is not None:
                return override
            if _entry_pins_model(group_entry):
                # Same rule one layer down: the group supplied the model
                # without asserting vision → the active_model flag
                # belongs to a different model.
                return None
        raw = await self._read_pref(_ACTIVE_MODEL_KEY, character=character)
        return parse_vision_override(raw if isinstance(raw, dict) else None)

    def _fallback_provider_id(self) -> str:
        """Return the runtime fallback provider for BYOK-first installs.

        New self-host installs boot with ``default_provider_id=fake`` so
        the app can start before provider keys exist. Once a DB-backed
        provider is registered, keeping fallback at fake would leave
        background LLM services disabled until an env/default knob was
        changed. Prefer the first registered non-fake provider in that
        case.
        """
        ids = self._registry.list_ids()
        if self._default_provider_id != _FAKE_PROVIDER_ID:
            if self._default_provider_id in ids:
                return self._default_provider_id
            first_real = _first_real_provider_id(ids)
            return first_real or _FAKE_PROVIDER_ID
        first_real = _first_real_provider_id(ids)
        return first_real or _FAKE_PROVIDER_ID

    def _provider_is_registered(self, provider_id: str) -> bool:
        return provider_id in self._registry.list_ids()

    async def _read_preferred_provider_id(
        self,
        feature_key: str | None,
        *,
        character: Character | None = None,
        content_tolerance: str | None = None,
    ) -> str | None:
        """Return the provider id picked for ``feature_key``, or ``None``
        when nothing is set (caller falls back to default).

        Resolution order: per-character override → global feature
        override → global ``active_model``. Malformed entries are
        treated as absent.
        """
        _, provider_id = await self._read_preferred_provider(
            feature_key,
            character=character,
            content_tolerance=content_tolerance,
        )
        return provider_id

    async def _read_preferred_provider(
        self,
        feature_key: str | None,
        *,
        character: Character | None = None,
        content_tolerance: str | None = None,
    ) -> tuple[str | None, str | None]:
        nsfw_target = await self._read_nsfw_target(character=character)
        if nsfw_target is not None:
            self._require_registered_nsfw_provider(nsfw_target.llm_provider_id)
            return ROUTING_SOURCE_NSFW_MODE, nsfw_target.llm_provider_id
        if _requires_community_tolerance(content_tolerance):
            nsfw_target = await self._read_configured_nsfw_target(
                character=character,
            )
            if nsfw_target is None:
                raise RuntimeError(
                    "nsfw content requires configured community LLM target",
                )
            self._require_registered_nsfw_provider(nsfw_target.llm_provider_id)
            return ROUTING_SOURCE_NSFW_CONTENT, nsfw_target.llm_provider_id
        if feature_key and character is not None:
            override = _per_character_override(character, feature_key)
            if override is not None and override.provider_id:
                return ROUTING_SOURCE_CHARACTER_FEATURE, override.provider_id
        if feature_key:
            feature_entry = await self._read_feature_entry(
                feature_key, character=character,
            )
            if feature_entry is not None:
                value = feature_entry.get("provider_id")
                if isinstance(value, str) and value.strip():
                    return ROUTING_SOURCE_GLOBAL_FEATURE, value
            group_entry = await self._read_group_entry(
                feature_key,
                character=character,
            )
            if group_entry is not None:
                value = group_entry.get("provider_id")
                if isinstance(value, str) and value.strip():
                    return ROUTING_SOURCE_GLOBAL_GROUP, value
        raw = await self._read_pref(_ACTIVE_MODEL_KEY, character=character)
        if isinstance(raw, dict):
            value = raw.get("provider_id")
            if isinstance(value, str) and value.strip():
                return ROUTING_SOURCE_ACTIVE_MODEL, value
        return None, None

    async def _read_feature_entry(
        self,
        feature_key: str,
        *,
        character: Character | None = None,
    ) -> dict[str, Any] | None:
        raw = await self._read_pref(_FEATURE_MODELS_KEY, character=character)
        if not isinstance(raw, dict):
            return None
        entry = raw.get(feature_key)
        if isinstance(entry, dict):
            return entry
        return None

    async def _read_group_entry(
        self,
        feature_key: str,
        *,
        character: Character | None = None,
    ) -> dict[str, Any] | None:
        group_key = FEATURE_TO_GROUP.get(feature_key)
        if group_key is None:
            return None
        raw = await self._read_pref(
            _FEATURE_MODEL_GROUPS_KEY,
            character=character,
        )
        if not isinstance(raw, dict):
            return None
        entry = raw.get(group_key)
        if isinstance(entry, dict):
            return entry
        return None

    async def _read_pref(
        self,
        key: str,
        *,
        character: Character | None = None,
    ) -> Any:
        try:
            user_id = getattr(character, "user_id", None) if character else None
            return await get_preference_with_user_fallback(
                self._preferences,
                key,
                user_id=user_id,
            )
        except Exception:
            _LOGGER.exception(
                "active LLM resolve: preferences read failed for %r; "
                "falling back to default",
                key,
            )
            return None

    async def _read_nsfw_target(self, *, character: Character | None) -> Any | None:
        if self._nsfw_mode_service is None or character is None:
            return None
        user_id = getattr(character, "user_id", None)
        return await self._nsfw_mode_service.active_target(user_id=user_id)

    async def _read_configured_nsfw_target(
        self, *, character: Character | None,
    ) -> Any | None:
        if self._nsfw_mode_service is None or character is None:
            return None
        user_id = getattr(character, "user_id", None)
        return await self._nsfw_mode_service.configured_target(user_id=user_id)

    def _require_registered_nsfw_provider(self, provider_id: str) -> None:
        if self._provider_is_registered(provider_id):
            return
        raise RuntimeError(
            "nsfw mode target provider is not registered; refusing fallback",
        )


def _per_character_override(
    character: Character, feature_key: str,
) -> FeatureModelOverride | None:
    """Defensive wrapper — domain method already ignores empty entries
    but we extra-guard against future shape changes here so the resolver
    stays the single fail-loud-or-fallthrough boundary."""
    return character.feature_model_for(feature_key)


def _entry_pins_model(entry: dict[str, Any] | None) -> bool:
    """Whether a routing entry pins a provider or model (vs. carrying
    only metadata like reasoning/vision). Uses the same non-empty-string
    coercion as model routing so the "did this layer supply the model?"
    test matches what the resolver actually routed on."""
    if not isinstance(entry, dict):
        return False
    for key in ("provider_id", "model_id"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return True
    return False


def _first_real_provider_id(provider_ids: list[str]) -> str | None:
    for provider_id in provider_ids:
        if provider_id != _FAKE_PROVIDER_ID:
            return provider_id
    return None


def _requires_community_tolerance(content_tolerance: str | None) -> bool:
    return normalize_content_tolerance(content_tolerance) == CONTENT_TOLERANCE_COMMUNITY
