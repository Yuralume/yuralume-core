"""Image-profile registry.

Holds the operator-defined :class:`ImageProfile` list and lazily
materialises one :class:`ImageProviderPort` instance per profile id.
Adapters live behind the same port so call sites stay
backend-agnostic — the registry is the only piece that knows how to
turn a ``ImageProfile`` row into an HTTP / ComfyUI client.

Caching matters here: the ComfyUI adapter pulls a workflow JSON off
disk and the OpenAI adapter is cheap-but-not-free to construct, so we
amortise the cost across the many auxiliary calls that route through
the active-image-provider during a single boot.

Lookups by missing id return ``None`` so the active-provider resolver
can fall through to the next preference layer instead of raising — a
stale preference (operator renamed a profile) should degrade to "use
the default profile" rather than 500ing the chat turn.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from kokoro_link.contracts.image_profile import ImageProfile
from kokoro_link.contracts.image_provider import ImageProviderPort
from kokoro_link.infrastructure.image.external_api_provider import (
    ExternalImageApiProvider,
)
from kokoro_link.infrastructure.image.gemini_provider import (
    GeminiImageProvider,
)
from kokoro_link.infrastructure.image.openai_provider import (
    OpenAIImageProvider,
)
from kokoro_link.infrastructure.image.openrouter_provider import (
    OpenRouterImageProvider,
)
from kokoro_link.infrastructure.image.xai_provider import (
    XAIImageProvider,
)
from kokoro_link.infrastructure.tools.comfyui.client import (
    AsyncComfyUiClient,
)
from kokoro_link.infrastructure.tools.comfyui.generator import (
    ComfyPortraitGenerator,
)
from kokoro_link.infrastructure.tools.comfyui.workflow import (
    DEFAULT_WORKFLOW_FILE,
    WorkflowBuilder,
)

if TYPE_CHECKING:
    from kokoro_link.contracts.prompt_rewriter import PromptRewriterPort


class ImageProfileRegistry:
    def __init__(
        self,
        profiles: list[ImageProfile],
        *,
        prompt_rewriter: "PromptRewriterPort | None" = None,
    ) -> None:
        # Preserve insertion order — the active-provider resolver uses
        # the first id as the fallback default when no preference layer
        # picks one, so operators control that pick by ordering their
        # JSON config.
        self._profiles: dict[str, ImageProfile] = {p.id: p for p in profiles}
        self._prompt_rewriter = prompt_rewriter
        self._cache: dict[str, ImageProviderPort] = {}

    @property
    def profile_ids(self) -> list[str]:
        return list(self._profiles.keys())

    @property
    def profiles(self) -> list[ImageProfile]:
        return list(self._profiles.values())

    def replace_profiles(self, profiles: list[ImageProfile]) -> None:
        self._profiles = {p.id: p for p in profiles}
        self._cache.clear()

    def get_profile(self, profile_id: str) -> ImageProfile | None:
        return self._profiles.get(profile_id)

    def resolve(self, profile_id: str) -> ImageProviderPort | None:
        """Return the built provider for ``profile_id`` or ``None``.

        Cached: the same profile id always returns the same instance
        for the life of the container — important because the ComfyUI
        adapter holds a workflow builder + httpx client whose setup
        cost we don't want to repeat on every chat turn.
        """
        cached = self._cache.get(profile_id)
        if cached is not None:
            return cached
        profile = self._profiles.get(profile_id)
        if profile is None:
            return None
        provider = self._build(profile)
        if provider is not None:
            self._cache[profile_id] = provider
        return provider

    def _build(self, profile: ImageProfile) -> ImageProviderPort | None:
        if profile.kind == "external_api":
            cfg = profile.api
            assert cfg is not None
            provider = cfg.provider.strip().lower()
            if provider in {"openai", "gpt_image", "gpt-image"}:
                return OpenAIImageProvider(
                    api_key=cfg.api_key,
                    model=cfg.model,
                    timeout_seconds=cfg.timeout_seconds,
                    base_url=cfg.base_url,
                )
            if provider in {"xai", "grok", "grok_image", "grok-image"}:
                return XAIImageProvider(
                    base_url=cfg.base_url,
                    api_key=cfg.api_key,
                    model=cfg.model,
                    timeout_seconds=cfg.timeout_seconds,
                )
            if provider == "openrouter":
                # OpenRouter's /api/v1/images shape differs from OpenAI's
                # /images/generations — its own adapter posts the right
                # path. Must precede the gateway fallback below.
                return OpenRouterImageProvider(
                    base_url=cfg.base_url,
                    api_key=cfg.api_key,
                    model=cfg.model,
                    timeout_seconds=cfg.timeout_seconds,
                )
            if provider in {
                "gemini",
                "google",
                "nano_banana",
                "nano-banana",
            }:
                return GeminiImageProvider(
                    base_url=cfg.base_url,
                    api_key=cfg.api_key,
                    model=cfg.model,
                    timeout_seconds=cfg.timeout_seconds,
                )
            return ExternalImageApiProvider(
                base_url=cfg.base_url,
                api_key=cfg.api_key,
                model=cfg.model,
                provider=cfg.provider,
                timeout_seconds=cfg.timeout_seconds,
            )
        if profile.kind == "comfyui":
            cfg = profile.comfyui
            assert cfg is not None  # invariant from ImageProfile.__post_init__
            if not cfg.server:
                return None
            import pathlib

            workflow_file = (
                pathlib.Path(cfg.workflow_file)
                if cfg.workflow_file
                else DEFAULT_WORKFLOW_FILE
            )
            client = AsyncComfyUiClient(
                server=cfg.server,
                generation_timeout=cfg.generation_timeout_seconds,
            )
            rewriter = (
                self._prompt_rewriter if cfg.use_prompt_rewriter else None
            )
            return ComfyPortraitGenerator(
                client=client,
                workflow_builder=WorkflowBuilder(workflow_file),
                checkpoint=cfg.checkpoint or None,
                prompt_rewriter=rewriter,
            )
        if profile.kind == "openai":
            cfg = profile.openai
            assert cfg is not None
            if not cfg.api_key:
                return None
            return OpenAIImageProvider(
                api_key=cfg.api_key,
                model=cfg.model,
                quality=cfg.quality,
                timeout_seconds=cfg.timeout_seconds,
                base_url=cfg.base_url,
            )
        return None
