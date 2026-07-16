"""Image-profile value objects.

A *profile* bundles everything needed to materialise an
``ImageProviderPort`` instance — the backend kind (hosted/external API
or legacy local adapters), the kind-specific config, plus a stable id callers
reference from preferences / per-character overrides.

Profiles are the "model" analog in the image-routing system, the way
``provider_id`` + ``model_id`` work in :class:`FeatureModelOverride`.
Defined statically by the operator (env / JSON config) so the same
ids round-trip through preferences and DB without surprise.

Why bundle workflow + checkpoint at the profile level instead of
exposing them as per-call kwargs:

  * The chat tool / portrait service / feed composer don't want to
    know which ComfyUI workflow to use; they just want "the style
    pinned for this character". Profiles let the operator define
    ``anime_local`` vs ``realistic_local`` once and route each
    character at the preference layer.

  * Tying ``workflow_file`` to ``checkpoint`` keeps incompatible combos
    impossible — an SDXL workflow can't accidentally be paired with an
    SD 1.5 checkpoint because they live in different profiles.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


ImageProfileKind = Literal["external_api", "comfyui", "openai"]


@dataclass(frozen=True, slots=True)
class ExternalImageApiProfileConfig:
    """Gateway/custom-wrapper image capability API profile.

    The core app sends a model id and prompt to this endpoint and receives
    image bytes through ``b64_json`` or a downloadable artifact URL. The
    default ``provider="gateway"`` means this profile speaks Kokoro-Link's
    normalized wire contract. Native provider names such as ``openai``,
    ``xai``, or ``gemini`` route to dedicated adapters because their
    request/response shapes differ.
    """

    base_url: str
    api_key: str
    model: str
    provider: str = "gateway"
    timeout_seconds: float = 180.0


@dataclass(frozen=True, slots=True)
class ComfyProfileConfig:
    """ComfyUI-flavoured profile knobs.

    Each ComfyUI profile is one (checkpoint, workflow, server) tuple.
    Same server can host several profiles — operators just point them
    at different workflow JSONs / checkpoint files. ``use_prompt_rewriter``
    lets a profile opt out of danbooru rewriting (useful for realistic
    checkpoints whose CLIP doesn't speak booru tag dialect)."""

    server: str
    checkpoint: str
    workflow_file: str = ""
    generation_timeout_seconds: float = 180.0
    use_prompt_rewriter: bool = True


@dataclass(frozen=True, slots=True)
class OpenAIProfileConfig:
    """OpenAI GPT Image 2 profile knobs.

    Operators usually define multiple profiles to expose different
    quality / cost tiers (e.g. ``openai_fast`` low-quality for the
    chat tool, ``openai_polished`` high-quality for portrait
    generation)."""

    api_key: str
    model: str = "gpt-image-2"
    quality: str = "medium"
    timeout_seconds: float = 180.0
    base_url: str = "https://api.openai.com/v1"


@dataclass(frozen=True, slots=True)
class ImageProfile:
    """A named, ready-to-build image provider config.

    Exactly one provider config is set, picked by ``kind``.
    The mismatch is a config error caught at parse time so call sites
    can rely on the invariant.
    """

    id: str
    label: str
    kind: ImageProfileKind
    api: ExternalImageApiProfileConfig | None = None
    comfyui: ComfyProfileConfig | None = None
    openai: OpenAIProfileConfig | None = None

    def __post_init__(self) -> None:
        pid = (self.id or "").strip()
        if not pid:
            raise ValueError("ImageProfile.id must be non-empty")
        if self.kind == "external_api" and self.api is None:
            raise ValueError(
                f"profile {pid!r}: kind=external_api requires api config",
            )
        if self.kind == "comfyui" and self.comfyui is None:
            raise ValueError(
                f"profile {pid!r}: kind=comfyui requires comfyui config",
            )
        if self.kind == "openai" and self.openai is None:
            raise ValueError(
                f"profile {pid!r}: kind=openai requires openai config",
            )


@dataclass(frozen=True, slots=True)
class FeatureImageProfileOverride:
    """Per-character override entry: pin ``feature_key`` to ``profile_id``.

    Mirrors :class:`FeatureModelOverride` so the persistence layer and
    update flows look identical between LLM and image routing. Empty
    ``profile_id`` is treated as "no override" so a stale row doesn't
    accidentally shadow the global pick — same fall-through semantics
    as the LLM override entries.
    """

    feature_key: str
    profile_id: str | None = None

    def __post_init__(self) -> None:
        key = (self.feature_key or "").strip()
        if not key:
            raise ValueError(
                "FeatureImageProfileOverride.feature_key must be non-empty",
            )
        object.__setattr__(self, "feature_key", key)
        profile = (self.profile_id or "").strip() or None
        object.__setattr__(self, "profile_id", profile)

    @property
    def is_empty(self) -> bool:
        return self.profile_id is None
