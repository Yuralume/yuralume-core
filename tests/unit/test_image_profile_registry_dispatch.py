"""Dispatch tests for :class:`ImageProfileRegistry._build`.

Pins which concrete adapter each ``ExternalImageApiProfileConfig.provider``
kind resolves to. The OpenRouter branch is load-bearing: a mis-ordered
or mis-spelled branch would silently fall through to the gateway
``ExternalImageApiProvider`` (wrong endpoint path) and only fail at HTTP
time, which is hard to debug.
"""

from __future__ import annotations

from kokoro_link.contracts.image_profile import (
    ExternalImageApiProfileConfig,
    ImageProfile,
)
from kokoro_link.infrastructure.image.external_api_provider import (
    ExternalImageApiProvider,
)
from kokoro_link.infrastructure.image.openrouter_provider import (
    OpenRouterImageProvider,
)
from kokoro_link.infrastructure.image.profile_registry import (
    ImageProfileRegistry,
)


def _profile(provider: str) -> ImageProfile:
    return ImageProfile(
        id="p", label="p", kind="external_api",
        api=ExternalImageApiProfileConfig(
            base_url="https://openrouter.ai/api/v1",
            api_key="sk-or-test",
            model="black-forest-labs/flux.2-pro",
            provider=provider,
        ),
    )


def test_openrouter_provider_kind_builds_openrouter_adapter() -> None:
    registry = ImageProfileRegistry([_profile("openrouter")])
    built = registry.resolve("p")
    assert isinstance(built, OpenRouterImageProvider)


def test_gateway_provider_kind_still_builds_external_adapter() -> None:
    registry = ImageProfileRegistry([_profile("gateway")])
    built = registry.resolve("p")
    assert isinstance(built, ExternalImageApiProvider)
    # OpenRouter must NOT hijack the gateway fallback path.
    assert not isinstance(built, OpenRouterImageProvider)
