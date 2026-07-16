"""Phase 0.1 contract-freeze guard: the cloud-mode external-ability route matrix.

Makes "cloud mode routes all paid generation through the Gateway" and "self-host
stays standalone" regression-guarded properties rather than prose. Cloud mode must
wire the ``Cloud*`` providers (which forward to the Gateway); self-host must wire the
preference-backed providers and must never construct a control-plane client.
"""

from __future__ import annotations

from kokoro_link.application.services.active_image_provider import (
    PreferenceBackedActiveImageProvider,
)
from kokoro_link.application.services.active_llm_provider import (
    PreferenceBackedActiveLLMProvider,
)
from kokoro_link.application.services.active_video_provider import (
    PreferenceBackedActiveVideoProvider,
)
from kokoro_link.application.services.cloud_active_llm_provider import (
    CloudActiveLLMProvider,
)
from kokoro_link.application.services.cloud_active_media_provider import (
    CloudActiveImageProvider,
    CloudActiveVideoProvider,
)
from kokoro_link.bootstrap.container import build_container
from kokoro_link.bootstrap.settings import AppSettings, CloudSettings
from kokoro_link.infrastructure.tts.cloud_gateway import CloudGatewayTTSAdapter
from kokoro_link.infrastructure.usage.llm_metering import MeteredActiveLLMProvider


def _cloud_settings() -> AppSettings:
    return AppSettings(
        cloud=CloudSettings(
            enabled=True,
            user_service_url="https://users.example",
            gateway_url="https://gateway.example",
            deployment_token="ykl_deploy",
            llm_model_presets={"chat": "preset-chat"},
        ),
    )


def test_cloud_mode_routes_all_generation_through_gateway() -> None:
    container = build_container(_cloud_settings())

    assert isinstance(container.active_llm_provider, MeteredActiveLLMProvider)
    assert isinstance(container.active_llm_provider.inner, CloudActiveLLMProvider)
    assert isinstance(
        container.character_image_service._image_provider,  # noqa: SLF001
        CloudActiveImageProvider,
    )
    assert isinstance(
        container.feed_composer_service._video_provider,  # noqa: SLF001
        CloudActiveVideoProvider,
    )
    assert isinstance(container.tts_service._port, CloudGatewayTTSAdapter)  # noqa: SLF001


def test_cloud_mode_never_wires_self_host_generation_providers() -> None:
    container = build_container(_cloud_settings())

    assert not isinstance(
        container.active_llm_provider.inner, PreferenceBackedActiveLLMProvider
    )
    assert not isinstance(
        container.character_image_service._image_provider,  # noqa: SLF001
        PreferenceBackedActiveImageProvider,
    )
    assert not isinstance(
        container.feed_composer_service._video_provider,  # noqa: SLF001
        PreferenceBackedActiveVideoProvider,
    )


def test_self_host_mode_keeps_preference_backed_providers() -> None:
    container = build_container(AppSettings())

    assert isinstance(container.active_llm_provider, MeteredActiveLLMProvider)
    assert isinstance(
        container.active_llm_provider.inner, PreferenceBackedActiveLLMProvider
    )
    assert isinstance(
        container.character_image_service._image_provider,  # noqa: SLF001
        PreferenceBackedActiveImageProvider,
    )
    assert isinstance(
        container.feed_composer_service._video_provider,  # noqa: SLF001
        PreferenceBackedActiveVideoProvider,
    )
    assert not isinstance(container.tts_service._port, CloudGatewayTTSAdapter)  # noqa: SLF001


def test_self_host_mode_constructs_no_control_plane_client() -> None:
    """Self-host isolation: the control-plane routing-profile resolver is never wired."""
    container = build_container(AppSettings())
    assert container.cloud_routing_profile_resolver is None


def test_cloud_runtime_config_mode_wires_routing_profile_resolver() -> None:
    """Cloud mode with the compat flag on wires the control-plane profile resolver."""
    settings = AppSettings(
        cloud=CloudSettings(
            enabled=True,
            user_service_url="https://users.example",
            gateway_url="https://gateway.example",
            deployment_token="ykl_deploy",
            runtime_config_enabled=True,
        ),
    )
    container = build_container(settings)
    assert container.cloud_routing_profile_resolver is not None


def test_cloud_mode_without_runtime_config_flag_uses_env_presets() -> None:
    """Cloud mode with the flag off keeps the env preset path (no profile resolver)."""
    settings = AppSettings(
        cloud=CloudSettings(
            enabled=True,
            user_service_url="https://users.example",
            gateway_url="https://gateway.example",
            deployment_token="ykl_deploy",
            llm_model_presets={"chat": "preset-chat"},
        ),
    )
    container = build_container(settings)
    assert container.cloud_routing_profile_resolver is None
