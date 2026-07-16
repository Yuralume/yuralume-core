"""Video-profile registry.

Holds the operator-defined :class:`VideoProfile` list and lazily
materialises one :class:`VideoProviderPort` per id. Cached because
the ComfyUI adapter loads a workflow JSON off disk + holds an httpx
client; the resolver hits it on every feed tick, so amortising the
setup matters.

Lookups by missing id return ``None`` so the active-provider resolver
can fall through instead of raising — a stale preference (operator
renamed a profile) should degrade to "use the default profile" rather
than failing the whole feed tick.
"""

from __future__ import annotations

from kokoro_link.contracts.video_profile import VideoProfile
from kokoro_link.contracts.video_provider import VideoProviderPort
from kokoro_link.infrastructure.video.external_api_provider import (
    ExternalVideoApiProvider,
)
from kokoro_link.infrastructure.video.google_veo_provider import (
    GoogleVeoVideoProvider,
)
from kokoro_link.infrastructure.tools.comfyui.client import (
    AsyncComfyUiClient,
)
from kokoro_link.infrastructure.tools.comfyui.video_generator import (
    ComfyVideoGenerator,
)
from kokoro_link.infrastructure.tools.comfyui.wan_video_workflow import (
    DEFAULT_WAN_VIDEO_WORKFLOW_FILE,
    WanVideoWorkflowBuilder,
)


class VideoProfileRegistry:
    def __init__(self, profiles: list[VideoProfile]) -> None:
        self._profiles: dict[str, VideoProfile] = {p.id: p for p in profiles}
        self._cache: dict[str, VideoProviderPort] = {}

    @property
    def profile_ids(self) -> list[str]:
        return list(self._profiles.keys())

    @property
    def profiles(self) -> list[VideoProfile]:
        return list(self._profiles.values())

    def replace_profiles(self, profiles: list[VideoProfile]) -> None:
        self._profiles = {p.id: p for p in profiles}
        self._cache.clear()

    def get_profile(self, profile_id: str) -> VideoProfile | None:
        return self._profiles.get(profile_id)

    def resolve(self, profile_id: str) -> VideoProviderPort | None:
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

    def _build(self, profile: VideoProfile) -> VideoProviderPort | None:
        if profile.kind == "external_api":
            cfg = profile.api
            assert cfg is not None
            provider = cfg.provider.strip().lower()
            if provider in {"google", "google_veo", "gemini_veo", "veo"}:
                return GoogleVeoVideoProvider(
                    base_url=cfg.base_url,
                    api_key=cfg.api_key,
                    model=cfg.model,
                    timeout_seconds=cfg.timeout_seconds,
                )
            return ExternalVideoApiProvider(
                base_url=cfg.base_url,
                api_key=cfg.api_key,
                model=cfg.model,
                provider=cfg.provider,
                timeout_seconds=cfg.timeout_seconds,
            )
        if profile.kind == "comfyui_wan22":
            cfg = profile.comfyui_wan22
            assert cfg is not None  # invariant from VideoProfile.__post_init__
            if not cfg.server:
                return None
            import pathlib

            workflow_file = (
                pathlib.Path(cfg.workflow_file)
                if cfg.workflow_file
                else DEFAULT_WAN_VIDEO_WORKFLOW_FILE
            )
            client = AsyncComfyUiClient(
                server=cfg.server,
                generation_timeout=cfg.generation_timeout_seconds,
            )
            return ComfyVideoGenerator(
                client=client,
                workflow_builder=WanVideoWorkflowBuilder(workflow_file),
                fps=cfg.fps,
                default_length_frames=cfg.length_frames,
                default_width=cfg.width,
                default_height=cfg.height,
            )
        return None
