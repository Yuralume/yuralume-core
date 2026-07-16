from __future__ import annotations

from collections.abc import Callable

from kokoro_link.application.services.cloud_identity_resolver import (
    CloudOperatorIdentityResolver,
)
from kokoro_link.contracts.active_image import ActiveImageProviderPort
from kokoro_link.contracts.active_video import ActiveVideoProviderPort
from kokoro_link.contracts.cloud_routing_profile import CloudRoutingProfilePort
from kokoro_link.contracts.cloud_gateway import CloudResourceContext
from kokoro_link.contracts.image_provider import ImageProviderPort
from kokoro_link.contracts.video_provider import VideoProviderPort
from kokoro_link.domain.entities.character import Character


_IMAGE_PROFILE_ID = "yuralume_cloud_image"
_VIDEO_PROFILE_ID = "yuralume_cloud_video"
_CAPABILITY_IMAGE = "image"
_CAPABILITY_VIDEO = "video"


class _ProfilePresetResolver:
    """Shared profile-driven preset resolution for cloud media providers.

    When a control-plane ``routing_profile_port`` is wired (cloud runtime-config
    mode), ``feature_key -> preset`` is resolved from the cached routing profile
    keyed by the character's tenant/account/tier; otherwise (or on a non-strict
    miss) the deprecated ``YURALUME_CLOUD_*_PRESET`` env default is the fallback.
    A strict (demo) account with no preset fails closed naming the feature key, so
    a demo never silently falls through to a paid env preset (plan §3).
    """

    def __init__(
        self,
        *,
        capability: str,
        identity_resolver: CloudOperatorIdentityResolver | None,
        routing_profile_port: CloudRoutingProfilePort | None,
        default_preset: str,
    ) -> None:
        self._capability = capability
        self._identity_resolver = identity_resolver
        self._routing_profile_port = routing_profile_port
        self._default_preset = default_preset

    async def resolve_preset(
        self, feature_key: str, *, character: Character | None
    ) -> str:
        if (
            self._routing_profile_port is not None
            and self._identity_resolver is not None
            and character is not None
        ):
            identity = await self._identity_resolver.resolve_context(
                CloudResourceContext.for_character(character),
            )
            profile = await self._routing_profile_port.get_profile(
                tenant_id=identity.tenant_id,
                account_id=identity.account_id,
                # User-scope override keyed by the player's cloud account id.
                # Set out-of-band by the cloud management plane (not Core); this
                # only reads whatever the control-plane resolved.
                user_id=identity.account_id,
                tier=identity.tenant_tier,
            )
            if profile.is_disabled(self._capability, feature_key):
                raise RuntimeError(
                    f"cloud {self._capability} feature {feature_key!r} is disabled "
                    f"by policy [{profile.source}]",
                )
            preset = profile.preset_for(self._capability, feature_key)
            if preset:
                return preset
            if profile.strict_no_fallback:
                raise RuntimeError(
                    f"cloud {self._capability} strict no-fallback requires an "
                    f"explicit preset for feature {feature_key!r} [{profile.source}]",
                )
            # Non-strict miss: fall through to the deprecated env preset default.
        return self._default_preset


class CloudActiveImageProvider(ActiveImageProviderPort):
    def __init__(
        self,
        *,
        provider_factory: Callable[[str, str], ImageProviderPort],
        identity_resolver: CloudOperatorIdentityResolver | None = None,
        routing_profile_port: CloudRoutingProfilePort | None = None,
        default_preset: str = "",
    ) -> None:
        self._provider_factory = provider_factory
        self._presets = _ProfilePresetResolver(
            capability=_CAPABILITY_IMAGE,
            identity_resolver=identity_resolver,
            routing_profile_port=routing_profile_port,
            default_preset=default_preset,
        )

    async def resolve(
        self,
        feature_key: str | None = None,
        *,
        character: Character | None = None,
    ) -> ImageProviderPort | None:
        resolved_feature_key = feature_key or "image"
        preset = await self._presets.resolve_preset(
            resolved_feature_key, character=character,
        )
        return self._provider_factory(resolved_feature_key, preset)

    async def resolve_profile_id(
        self,
        feature_key: str | None = None,
        *,
        character: Character | None = None,
    ) -> str | None:
        return _IMAGE_PROFILE_ID


class CloudActiveVideoProvider(ActiveVideoProviderPort):
    def __init__(
        self,
        *,
        provider_factory: Callable[[str, str], VideoProviderPort],
        identity_resolver: CloudOperatorIdentityResolver | None = None,
        routing_profile_port: CloudRoutingProfilePort | None = None,
        default_preset: str = "",
    ) -> None:
        self._provider_factory = provider_factory
        self._presets = _ProfilePresetResolver(
            capability=_CAPABILITY_VIDEO,
            identity_resolver=identity_resolver,
            routing_profile_port=routing_profile_port,
            default_preset=default_preset,
        )

    async def resolve(
        self,
        feature_key: str | None = None,
        *,
        character: Character | None = None,
    ) -> VideoProviderPort | None:
        resolved_feature_key = feature_key or "video"
        preset = await self._presets.resolve_preset(
            resolved_feature_key, character=character,
        )
        return self._provider_factory(resolved_feature_key, preset)

    async def resolve_profile_id(
        self,
        feature_key: str | None = None,
        *,
        character: Character | None = None,
    ) -> str | None:
        return _VIDEO_PROFILE_ID
