"""Video-profile value objects.

Same idea as :mod:`kokoro_link.contracts.image_profile` but for the
video side: a named bundle of (backend kind, kind-specific config)
the operator declares once and references from preferences /
per-character overrides.

Kept in its own module rather than crammed into ``image_profile`` so
``video`` doesn't accidentally inherit image-side semantics (aspect
WH tables, danbooru-rewriter coupling) that don't apply to Wan2.2.

Current deployment-facing path is ``external_api``: a normalized
gateway/custom-wrapper video capability API that may route to Veo, xAI, or
another service behind the gateway. ``comfyui_wan22`` is kept only for legacy
local-dev profile compatibility.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


VideoProfileKind = Literal["external_api", "comfyui_wan22"]


@dataclass(frozen=True, slots=True)
class ExternalVideoApiProfileConfig:
    """Hosted video capability API profile.

    ``provider="gateway"`` speaks Kokoro-Link's normalized wrapper contract;
    ``provider="google_veo"`` routes to Gemini API's native Veo adapter.
    """

    base_url: str
    api_key: str
    model: str
    provider: str = "gateway"
    timeout_seconds: float = 1800.0


@dataclass(frozen=True, slots=True)
class WanVideoProfileConfig:
    """ComfyUI Wan2.2 text-to-video profile knobs.

    ``workflow_file`` is the path to the ComfyUI API-format JSON the
    operator exported from the Wan2.2 t2v graph. We ship a reference
    workflow at ``src/kokoro_link/infrastructure/tools/comfyui/workflows/
    wan22_t2v.json`` that's the baseline; operators with a tuned graph
    (different sampler / LoRA / step count) can point at their own.

    ``fps`` × ``length_frames`` controls clip duration. Default 16 fps
    × 81 frames ≈ 5 seconds, matching what the reference workflow ships.
    """

    server: str
    workflow_file: str = ""
    fps: int = 16
    length_frames: int = 81
    width: int = 832
    height: int = 480
    """Default dimensions match the reference workflow. Per-call
    ``aspect`` (portrait/landscape/square) overrides these."""
    generation_timeout_seconds: float = 1800.0
    """Wan2.2 14B on a single Blackwell GPU is ~10-15 minutes per clip.
    Default leans generous; operators on hosted GPUs can shrink it."""


@dataclass(frozen=True, slots=True)
class VideoProfile:
    id: str
    label: str
    kind: VideoProfileKind
    api: ExternalVideoApiProfileConfig | None = None
    comfyui_wan22: WanVideoProfileConfig | None = None

    def __post_init__(self) -> None:
        pid = (self.id or "").strip()
        if not pid:
            raise ValueError("VideoProfile.id must be non-empty")
        if self.kind == "external_api" and self.api is None:
            raise ValueError(
                f"profile {pid!r}: kind=external_api requires api config",
            )
        if self.kind == "comfyui_wan22" and self.comfyui_wan22 is None:
            raise ValueError(
                f"profile {pid!r}: kind=comfyui_wan22 requires comfyui_wan22 config",
            )


@dataclass(frozen=True, slots=True)
class FeatureVideoProfileOverride:
    """Per-character override entry: pin ``feature_key`` to ``profile_id``.

    Same shape as :class:`FeatureImageProfileOverride` so persistence /
    DTO / UI plumbing can stay symmetrical."""

    feature_key: str
    profile_id: str | None = None

    def __post_init__(self) -> None:
        key = (self.feature_key or "").strip()
        if not key:
            raise ValueError(
                "FeatureVideoProfileOverride.feature_key must be non-empty",
            )
        object.__setattr__(self, "feature_key", key)
        profile = (self.profile_id or "").strip() or None
        object.__setattr__(self, "profile_id", profile)

    @property
    def is_empty(self) -> bool:
        return self.profile_id is None
