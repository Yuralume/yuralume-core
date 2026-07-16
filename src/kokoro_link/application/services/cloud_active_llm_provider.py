from __future__ import annotations

from collections.abc import Callable

from kokoro_link.application.services.account_runtime_profile import (
    PermissiveAccountRuntimeProfileResolver,
)
from kokoro_link.application.services.cloud_identity_context import (
    current_cloud_actor,
)
from kokoro_link.application.services.cloud_identity_resolver import (
    CloudOperatorIdentityResolver,
)
from kokoro_link.contracts.account_runtime_profile import (
    AccountRuntimeProfileResolverPort,
)
from kokoro_link.contracts.cloud_gateway import (
    CloudGatewayIdentity,
    CloudResourceContext,
)
from kokoro_link.contracts.cloud_routing_profile import CloudRoutingProfilePort
from kokoro_link.contracts.active_llm import ActiveLLMProviderPort
from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.domain.entities.character import Character


_PROVIDER_ID = "yuralume_cloud"
_DEFAULT_MODEL_PRESET = "yuralume-default"
_CAPABILITY = "llm"


class CloudActiveLLMProvider(ActiveLLMProviderPort):
    """Route all hosted-core LLM calls through Yuralume Cloud Gateway.

    When a control-plane ``routing_profile_port`` is wired (cloud runtime-config
    mode), ``feature_key -> preset`` is resolved from the cached routing profile;
    otherwise the deprecated ``YURALUME_CLOUD_LLM_PRESETS`` env map is the fallback.
    """

    def __init__(
        self,
        *,
        identity_resolver: CloudOperatorIdentityResolver,
        model_factory: Callable[
            [str, CloudGatewayIdentity | None, str],
            ChatModelPort,
        ],
        model_presets: dict[str, str] | None = None,
        account_runtime_profile_resolver: (
            AccountRuntimeProfileResolverPort | None
        ) = None,
        routing_profile_port: CloudRoutingProfilePort | None = None,
    ) -> None:
        self._identity_resolver = identity_resolver
        self._model_factory = model_factory
        self._model_presets = dict(model_presets or {})
        self._account_runtime_profile_resolver = (
            account_runtime_profile_resolver
            or PermissiveAccountRuntimeProfileResolver()
        )
        self._routing_profile_port = routing_profile_port

    async def resolve(
        self,
        feature_key: str | None = None,
        *,
        character: Character | None = None,
        operator_id: str | None = None,
        content_tolerance: str | None = None,
    ) -> ChatModelPort:
        _ = content_tolerance
        character, operator_id = self._merge_ambient(character, operator_id)
        resolved_feature_key = feature_key or "chat"
        identity = await self._resolve_identity(
            character=character,
            operator_id=operator_id,
        )
        preset = await self._resolve_preset(
            resolved_feature_key,
            identity=identity,
            character=character,
            operator_id=operator_id,
        )
        return self._model_factory(resolved_feature_key, identity, preset)

    async def resolve_model_id(
        self,
        feature_key: str | None = None,
        *,
        character: Character | None = None,
        operator_id: str | None = None,
        content_tolerance: str | None = None,
    ) -> str | None:
        _ = content_tolerance
        character, operator_id = self._merge_ambient(character, operator_id)
        resolved_feature_key = feature_key or "chat"
        identity = (
            await self._resolve_identity(
                character=character,
                operator_id=operator_id,
            )
            if self._routing_profile_port is not None
            else None
        )
        return await self._resolve_preset(
            resolved_feature_key,
            identity=identity,
            character=character,
            operator_id=operator_id,
        )

    async def _resolve_preset(
        self,
        feature_key: str,
        *,
        identity: CloudGatewayIdentity | None,
        character: Character | None,
        operator_id: str | None,
    ) -> str:
        if self._routing_profile_port is not None and identity is not None:
            profile = await self._routing_profile_port.get_profile(
                tenant_id=identity.tenant_id,
                account_id=identity.account_id,
                # The player's user-scope override is keyed by their cloud account id.
                # Overrides are set out-of-band by the cloud management plane (not by
                # Core); this only reads whatever the control-plane resolved.
                user_id=identity.account_id,
                tier=identity.tenant_tier,
            )
            if profile.is_disabled(_CAPABILITY, feature_key):
                raise RuntimeError(
                    f"cloud LLM feature {feature_key!r} is disabled by policy "
                    f"[{profile.source}]",
                )
            preset = profile.preset_for(_CAPABILITY, feature_key)
            if preset:
                return preset
            if profile.strict_no_fallback:
                raise RuntimeError(
                    "cloud LLM strict no-fallback requires an explicit preset "
                    f"for feature {feature_key!r} [{profile.source}]",
                )
            # Non-strict miss: fall through to the deprecated env preset map.
        strict_no_fallback = await self._strict_no_fallback(
            character=character,
            operator_id=operator_id,
        )
        return self._preset_for(feature_key, strict_no_fallback=strict_no_fallback)

    async def is_fake(
        self,
        feature_key: str | None = None,
        *,
        character: Character | None = None,
        operator_id: str | None = None,
        content_tolerance: str | None = None,
    ) -> bool:
        _ = feature_key, character, operator_id, content_tolerance
        # Cloud routing never resolves to the in-process fake model; the
        # ambient actor is irrelevant here, so no merge is needed.
        return False

    @staticmethod
    def _merge_ambient(
        character: Character | None,
        operator_id: str | None,
    ) -> tuple[Character | None, str | None]:
        """Overlay the ambient cloud actor only when the caller passed no
        explicit routing context. Explicit character / operator_id always
        win; the ambient value is the identity fallback for leaf services
        that don't (and shouldn't) thread it."""
        if character is not None or (operator_id or "").strip():
            return character, operator_id
        ambient = current_cloud_actor()
        if ambient is None:
            return character, operator_id
        return ambient.character, ambient.operator_id

    async def _resolve_identity(
        self,
        *,
        character: Character | None,
        operator_id: str | None,
    ) -> CloudGatewayIdentity | None:
        context = self._resource_context(
            character=character,
            operator_id=operator_id,
        )
        if context is None:
            return None
        return await self._identity_resolver.resolve_context(context)

    @staticmethod
    def _resource_context(
        *,
        character: Character | None,
        operator_id: str | None,
    ) -> CloudResourceContext | None:
        if character is not None:
            return CloudResourceContext.for_character(character)
        if operator_id is not None and operator_id.strip():
            return CloudResourceContext.for_account(operator_id)
        return None

    async def _strict_no_fallback(
        self,
        *,
        character: Character | None,
        operator_id: str | None,
    ) -> bool:
        resolved_operator_id = (
            character.user_id if character is not None else (operator_id or "")
        ).strip()
        if not resolved_operator_id:
            return False
        profile = await self._account_runtime_profile_resolver.resolve_for_operator(
            resolved_operator_id,
        )
        return bool(profile.strict_no_fallback)

    def _preset_for(
        self,
        feature_key: str,
        *,
        strict_no_fallback: bool = False,
    ) -> str:
        preset = self._model_presets.get(feature_key)
        if preset:
            return preset
        if strict_no_fallback:
            raise RuntimeError(
                "cloud LLM strict no-fallback requires an explicit preset "
                f"for feature {feature_key!r}",
            )
        return self._model_presets.get("default") or _DEFAULT_MODEL_PRESET
