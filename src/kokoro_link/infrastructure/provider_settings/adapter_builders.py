"""Row→adapter builders shared by runtime sync and the live probe.

There must be exactly ONE mapping from a persisted/draft
:class:`ProviderConnection` (plus its decrypted secret) to a runtime
adapter. ``runtime_sync`` consumes these builders at boot/save time to
wire the live registries, and ``live_probe`` consumes the SAME builders
to construct a throwaway adapter from a draft and run its optional
probe hook — so a request-shape quirk can never again be fixed in the
adapter but missed in the probe (or vice versa; see the
``max_tokens``→``max_completion_tokens`` incident).

Import discipline: this module must never import ``bootstrap.*``. The
bootstrap container reaches ``live_probe`` through the provider
connection service, and ``live_probe`` imports this module at module
level — a bootstrap import here would close that cycle and crash at
boot (``runtime_sync`` keeps its container import; it is only ever
imported lazily from here-adjacent code paths).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from kokoro_link.contracts.image_profile import (
    ComfyProfileConfig,
    ExternalImageApiProfileConfig,
    ImageProfile,
)
from kokoro_link.contracts.provider_settings import ProviderConnection
from kokoro_link.contracts.video_profile import (
    ExternalVideoApiProfileConfig,
    VideoProfile,
)
from kokoro_link.infrastructure.embedder.lm_studio import LMStudioEmbedder
from kokoro_link.infrastructure.llm.anthropic import AnthropicChatModel
from kokoro_link.infrastructure.llm.openai_compatible import OpenAICompatibleChatModel
from kokoro_link.infrastructure.persistence.models import MEMORY_EMBEDDING_DIM
from kokoro_link.infrastructure.tts.external_api import (
    ExternalTTSAdapter,
    OpenAITTSAdapter,
    OpenRouterTTSAdapter,
)
from kokoro_link.infrastructure.tools.websearch import (
    DuckDuckGoSearchClient,
    OpenAIWebSearchClient,
    SearchClientPort,
    SearXNGSearchClient,
    TavilyClient,
)

_LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Provider defaults (single source of truth for both sync and probe)
# ---------------------------------------------------------------------------

# Providers whose /audio/speech endpoint speaks the OpenAI speech
# protocol (so they ride the OpenAI-speech adapter family rather than
# the generic gateway ExternalTTSAdapter). OpenRouter's synth endpoint
# is protocol-compatible but its voice catalog is per-model
# (supported_voices from GET /models?output_modalities=speech) and its
# response_format set is {mp3, pcm} → dedicated subclass.
_OPENAI_SPEECH_PROTOCOL_ADAPTERS: dict[str, type[OpenAITTSAdapter]] = {
    "openai": OpenAITTSAdapter,
    "openrouter": OpenRouterTTSAdapter,
}
_OPENAI_SPEECH_PROTOCOL_PROVIDERS = frozenset(_OPENAI_SPEECH_PROTOCOL_ADAPTERS)

_OPENAI_COMPATIBLE_DEFAULTS: dict[str, tuple[str, str]] = {
    "openai": ("https://api.openai.com/v1", "gpt-4o-mini"),
    # gemini-2.0-flash was hard shut down 2026-06-01; gemini-3.5-flash is the
    # live successor (skips the 2026-10-16 retirement of gemini-2.5-flash).
    "google_gemini": (
        "https://generativelanguage.googleapis.com/v1beta/openai",
        "gemini-3.5-flash",
    ),
    "openrouter": ("https://openrouter.ai/api/v1", "openai/gpt-4o-mini"),
    # NanoGPT dropped the bare 'gpt-5.2' alias; canonical id is 'openai/gpt-5.2'.
    "nanogpt": ("https://nano-gpt.com/api/v1", "openai/gpt-5.2"),
    # 'deepseek-chat' retires 2026-07-24 → its alias target 'deepseek-v4-flash'.
    "deepseek": ("https://api.deepseek.com/v1", "deepseek-v4-flash"),
    "mistral": ("https://api.mistral.ai/v1", "mistral-small-latest"),
    "custom_openai_compatible": ("", ""),
    "local_openai_compatible": ("http://127.0.0.1:1234/v1", "local-model"),
    "yuralume_cloud": ("", ""),
}


def default_base_url_for(provider_id: str) -> str:
    """Best-known API base for a catalog provider.

    Model discovery uses this when a draft connection omits ``base_url``,
    mirroring the runtime adapters which apply the same default at sync
    time — so "leave the field empty for the provider default" holds for
    both saving and the fetch-models probe. Providers without a known
    base (custom / yuralume_cloud) return ``""`` and keep the explicit
    "base_url is required" discovery error.
    """
    defaults = _OPENAI_COMPATIBLE_DEFAULTS.get(provider_id)
    return defaults[0] if defaults else ""


_IMAGE_DEFAULTS: dict[str, tuple[str, str, str]] = {
    "openai": ("https://api.openai.com/v1", "gpt-image-2", "openai"),
    # gemini-2.5-flash-image shuts down 2026-10-02; Google's announced
    # replacement is gemini-3.1-flash-image-preview (deprecations page,
    # verified 2026-07-16).
    "google_gemini": (
        "https://generativelanguage.googleapis.com/v1beta",
        "gemini-3.1-flash-image-preview",
        "gemini",
    ),
    # grok-2-image-1212 is legacy (absent from docs.x.ai/developers/models)
    # and rejects the grok-imagine-era aspect_ratio param — default to the
    # current model, aligned with catalog + XAIImageProvider ctor
    # (verified 2026-07-16).
    "xai": ("https://api.x.ai/v1", "grok-imagine-image-quality", "xai"),
    # OpenRouter posts /api/v1/images (not /images/generations) → its own
    # provider kind routes to OpenRouterImageProvider (verified 2026-07-05).
    "openrouter": ("https://openrouter.ai/api/v1", "black-forest-labs/flux.2-pro", "openrouter"),
    # NanoGPT's OpenAI-compatible /v1/images/generations returns b64_json →
    # gateway kind rides ExternalImageApiProvider (verified 2026-07-05).
    # 'flux-1.1-pro' vanished from GET /api/v1/image-models (the FLUX ids
    # are now 'flux-pro/v1.1', whose resolutions also lack our portrait/
    # landscape sizes); gpt-image-1 supports the exact
    # {1024x1024,1024x1536,1536x1024} triplet with max_images 4
    # (catalog re-verified 2026-07-16).
    "nanogpt": ("https://nano-gpt.com/api/v1", "gpt-image-1", "gateway"),
    "custom_media_gateway": ("", "", "gateway"),
    "yuralume_cloud": ("", "", "gateway"),
}

_VIDEO_DEFAULTS: dict[str, tuple[str, str, str]] = {
    "google_veo": (
        "https://generativelanguage.googleapis.com/v1beta",
        "veo-3.1-generate-preview",
        "google_veo",
    ),
    "custom_media_gateway": ("", "", "gateway"),
    "yuralume_cloud": ("", "", "gateway"),
}

# (base_url, model, voice, response_format). The format default is
# provider-scoped because OpenRouter's /audio/speech schema-validates
# response_format to {mp3, pcm} (wav is rejected with a ZodError before
# auth — live-verified 2026-07-16), while direct OpenAI supports wav.
# The OpenRouter default model/voice come from its authoritative speech
# catalog (GET /models?output_modalities=speech, checked 2026-07-16):
# no openai/* TTS model is listed there any more, so the old
# 'openai/gpt-4o-mini-tts' + 'alloy' default could not resolve.
_TTS_DEFAULTS: dict[str, tuple[str, str, str, str]] = {
    "openai": ("https://api.openai.com/v1", "gpt-4o-mini-tts", "marin", "wav"),
    "openrouter": (
        "https://openrouter.ai/api/v1",
        "x-ai/grok-voice-tts-1.0",
        "eve",
        "mp3",
    ),
    "custom_tts": ("", "", "", ""),
    "yuralume_cloud": ("", "", "", ""),
}

# Providers wired for the `search` capability → build the `web_search`
# ToolPort. Membership here (not per-provider branching) is the switch:
# a search row whose provider isn't in this set is recorded as
# unimplemented and skipped.
_SEARCH_PROVIDERS = frozenset(
    {"tavily", "searxng", "duckduckgo", "openai_web_search"},
)

_EMBEDDING_DEFAULTS: dict[str, tuple[str, str, bool]] = {
    "openai": ("https://api.openai.com/v1", "text-embedding-3-small", True),
    # baai/bge-m3 outputs 1024 dims natively → satisfies the
    # MEMORY_EMBEDDING_DIM hard constraint without dimensions truncation
    # (verified 2026-07-05). request_dimensions defaults False accordingly.
    "openrouter": ("https://openrouter.ai/api/v1", "baai/bge-m3", False),
    "custom_openai_compatible": ("", "", False),
    "local_openai_compatible": (
        "http://127.0.0.1:1234/v1",
        "text-embedding-bge-m3",
        False,
    ),
    "yuralume_cloud": ("", "text-embedding-bge-m3", False),
}


# ---------------------------------------------------------------------------
# Config coercion helpers (shared with runtime_sync)
# ---------------------------------------------------------------------------


def _config_str(
    row: ProviderConnection,
    key: str,
    default: str,
) -> str:
    value = row.config.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return default


def _config_bool(
    row: ProviderConnection,
    key: str,
    default: bool,
) -> bool:
    value = row.config.get(key)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return default


def _config_int(
    row: ProviderConnection,
    key: str,
    default: int,
) -> int:
    value = _config_optional_int(row, key)
    return default if value is None else value


def _config_optional_int(row: ProviderConnection, key: str) -> int | None:
    value = row.config.get(key)
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _config_optional_str(row: ProviderConnection, key: str) -> str | None:
    """Return a trimmed non-empty string, else ``None`` (i.e. "unset").

    Mirrors ``_config_optional_int`` so reasoning-effort passthrough sends
    nothing when the operator leaves the field blank."""
    value = row.config.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _config_optional_json_object(
    row: ProviderConnection,
    key: str,
) -> dict | None:
    """Parse a config string as a JSON *object* for the escape-hatch field.

    Fail-soft: a blank value, malformed JSON, or a non-object (array,
    scalar) yields ``None`` and a warning rather than raising — a bad
    escape-hatch entry must never take the whole provider offline."""
    value = row.config.get(key)
    if isinstance(value, dict):
        return value or None
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = json.loads(value)
    except (ValueError, TypeError):
        _LOGGER.warning(
            "provider %s: %s is not valid JSON; ignoring",
            row.provider,
            key,
        )
        return None
    if not isinstance(parsed, dict):
        _LOGGER.warning(
            "provider %s: %s must be a JSON object; ignoring",
            row.provider,
            key,
        )
        return None
    return parsed or None


def _optional_secret(secret: dict[str, Any], key: str) -> str | None:
    value = secret.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


# ---------------------------------------------------------------------------
# llm
# ---------------------------------------------------------------------------


def build_chat_model(
    row: ProviderConnection,
    secret: dict[str, Any],
):
    """Build the chat adapter for an ``llm`` row (or ``None`` if unknown).

    Raises ``ValueError`` when required config/secret is missing — the
    runtime sync records it as a runtime status, the live probe surfaces
    it as a failed ``config_check`` report.
    """
    if row.provider == "anthropic":
        api_key = str(secret.get("api_key") or "")
        if not api_key:
            raise ValueError("Anthropic provider requires api_key")
        return AnthropicChatModel(
            api_key=api_key,
            base_url=_config_str(row, "base_url", "https://api.anthropic.com"),
            model=_config_str(row, "default_model", "claude-sonnet-4-5"),
            anthropic_version=_config_str(row, "anthropic_version", "2023-06-01"),
            # Keep default True: every Anthropic chat model in the catalog
            # is multimodal, so a key-absent Anthropic row is safely
            # vision-capable. Do NOT "fix" this to match the
            # openai_compatible False default below — the asymmetry is
            # intentional (aggregators serve text-only models; Claude
            # doesn't).
            supports_vision=_config_bool(row, "supports_vision", True),
            max_tokens=_config_int(row, "max_tokens", 4096),
            thinking_budget_tokens=_config_optional_int(
                row, "thinking_budget_tokens",
            ),
        )
    if row.provider in _OPENAI_COMPATIBLE_DEFAULTS:
        default_base_url, default_model = _OPENAI_COMPATIBLE_DEFAULTS[row.provider]
        base_url = _config_str(row, "base_url", default_base_url)
        model = _config_str(row, "default_model", default_model)
        if not base_url or not model:
            raise ValueError("OpenAI-compatible provider requires base_url and default_model")
        return OpenAICompatibleChatModel(
            provider_id=row.provider,
            base_url=base_url,
            api_key=_optional_secret(secret, "api_key"),
            model=model,
            # Default False: a key-absent row means the operator never
            # asserted vision. Assuming True mislabels text-only models
            # (e.g. an OpenRouter deepseek route) as vision-capable, so
            # images get attached and the upstream hard-rejects the image
            # parts (404 "No endpoints found that support image input").
            supports_vision=_config_bool(row, "supports_vision", False),
            max_tokens=_config_optional_int(row, "max_tokens"),
            disable_reasoning=_config_bool(row, "disable_reasoning", False),
            reasoning_effort=_config_optional_str(row, "reasoning_effort"),
            extra_request_params=_config_optional_json_object(
                row, "extra_request_params",
            ),
            strip_think_tags=_config_bool(row, "strip_think_tags", False),
        )
    return None


# ---------------------------------------------------------------------------
# embedding
# ---------------------------------------------------------------------------


def build_embedder(
    row: ProviderConnection,
    secret: dict[str, Any],
) -> LMStudioEmbedder | None:
    """Build the embedding adapter for an ``embedding`` row.

    ``None`` when the provider has no embedding wiring; ``ValueError``
    when required config is missing or the configured dimension can't
    feed the fixed-width memory store column.
    """
    if row.provider not in _EMBEDDING_DEFAULTS:
        return None
    default_base_url, default_model, default_request_dimensions = (
        _EMBEDDING_DEFAULTS[row.provider]
    )
    base_url = _config_str(row, "base_url", default_base_url)
    model = _config_str(
        row,
        "embedding_model",
        _config_str(row, "default_model", default_model),
    )
    if not base_url or not model:
        raise ValueError("embedding provider requires base_url and embedding_model")
    dimension = _config_int(row, "embedding_dimension", MEMORY_EMBEDDING_DIM)
    if dimension != MEMORY_EMBEDDING_DIM:
        raise ValueError(
            "embedding_dimension must match memory_items.embedding "
            f"vector({MEMORY_EMBEDDING_DIM})",
        )
    return LMStudioEmbedder(
        base_url=base_url,
        api_key=_optional_secret(secret, "api_key"),
        model=model,
        dimension=dimension,
        timeout_seconds=float(_config_int(row, "timeout_seconds", 30)),
        request_dimensions=_config_bool(
            row,
            "request_dimensions",
            default_request_dimensions,
        ),
    )


# ---------------------------------------------------------------------------
# tts
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BuiltTTS:
    """A TTS port plus the resolved values runtime_sync needs for
    ``TTSSettings`` (kept here so both consumers resolve config the
    same way exactly once)."""

    port: ExternalTTSAdapter
    provider_kind: str  # "openai" (speech protocol family) | "custom"
    base_url: str
    api_key: str
    model: str
    voice_id: str
    response_format: str
    timeout_seconds: float


def build_tts(
    row: ProviderConnection,
    secret: dict[str, Any],
) -> BuiltTTS | None:
    """Build the TTS adapter for a ``tts`` row (or ``None`` if unknown).

    Raises ``ValueError`` when required config/secret is missing (e.g.
    the OpenAI speech family requires an api_key, custom TTS a base_url).
    """
    if row.provider not in _TTS_DEFAULTS:
        return None
    default_base_url, default_model, default_voice, default_format = (
        _TTS_DEFAULTS[row.provider]
    )
    base_url = _config_str(row, "base_url", default_base_url)
    model = _config_str(
        row,
        "tts_model",
        _config_str(row, "default_model", default_model),
    )
    voice_id = _config_str(row, "voice_id", default_voice)
    api_key = _optional_secret(secret, "api_key") or ""
    timeout = float(_config_int(row, "timeout_seconds", 90))
    adapter_cls = _OPENAI_SPEECH_PROTOCOL_ADAPTERS.get(row.provider)
    if adapter_cls is not None:
        response_format = _config_str(
            row, "response_format", default_format or "wav",
        )
        port = adapter_cls(
            api_key=api_key,
            base_url=base_url,
            model=model or default_model,
            default_voice_id=voice_id or default_voice,
            response_format=response_format,
            timeout_seconds=timeout,
        )
        return BuiltTTS(
            port=port,
            provider_kind="openai",
            base_url=base_url,
            api_key=api_key,
            model=model,
            voice_id=voice_id,
            response_format=response_format,
            timeout_seconds=timeout,
        )
    if not base_url:
        raise ValueError("custom TTS provider requires base_url")
    port = ExternalTTSAdapter(
        base_url=base_url,
        api_key=api_key,
        default_voice_id=voice_id,
        timeout_seconds=timeout,
    )
    return BuiltTTS(
        port=port,
        provider_kind="custom",
        base_url=base_url,
        api_key=api_key,
        model=model,
        voice_id=voice_id,
        response_format="",
        timeout_seconds=timeout,
    )


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


def build_search_client(
    row: ProviderConnection,
    secret: dict[str, Any],
) -> SearchClientPort:
    """Build the concrete search client for an enabled `search` row.

    Dispatch is on ``row.provider`` (already gated to ``_SEARCH_PROVIDERS``
    by the caller). Each branch reads its own config keys and maps missing
    requireds to a ``ValueError`` the caller turns into a runtime status."""
    timeout = float(_config_int(row, "timeout_seconds", 15))
    if row.provider == "tavily":
        api_key = _optional_secret(secret, "api_key")
        if not api_key:
            raise ValueError("Tavily search requires api_key")
        return TavilyClient(
            api_key=api_key,
            base_url=_config_str(row, "base_url", "https://api.tavily.com"),
            search_depth=_config_str(row, "search_depth", "advanced"),
            timeout_seconds=timeout,
        )
    if row.provider == "searxng":
        # Catalog field key is ``searxng_base_url`` (so the admin form can
        # surface SearXNG-specific i18n guidance); fall back to the legacy
        # ``base_url`` key for rows saved before that rename.
        base_url = _config_str(row, "searxng_base_url", "") or _config_str(
            row, "base_url", ""
        )
        if not base_url:
            raise ValueError("SearXNG search requires base_url")
        return SearXNGSearchClient(
            base_url=base_url,
            api_key=_optional_secret(secret, "api_key"),
            timeout_seconds=timeout,
        )
    if row.provider == "openai_web_search":
        api_key = _optional_secret(secret, "api_key")
        if not api_key:
            raise ValueError("OpenAI web search requires api_key")
        model = _config_str(row, "search_model", "")
        if not model:
            raise ValueError("OpenAI web search requires search_model")
        return OpenAIWebSearchClient(
            api_key=api_key,
            model=model,
            base_url=_config_str(row, "base_url", "https://api.openai.com/v1"),
            tool_type=_config_str(row, "search_tool_type", "web_search"),
            search_context_size=_config_optional_str(row, "search_context_size"),
            # LLM-native search does live fetch + synthesis → slower than the
            # REST search APIs; default to a longer timeout than the 15s above.
            timeout_seconds=float(_config_int(row, "timeout_seconds", 30)),
        )
    # duckduckgo — no auth, no base_url (Instant Answer public endpoint).
    return DuckDuckGoSearchClient(timeout_seconds=timeout)


# ---------------------------------------------------------------------------
# image
# ---------------------------------------------------------------------------


def build_image_profile(
    row: ProviderConnection,
    secret: dict[str, Any],
) -> ImageProfile:
    """Build the ImageProfile for a non-ComfyUI ``image`` row.

    Raises ``ValueError`` for unsupported providers or missing config —
    the runtime sync records it as a runtime status."""
    if row.provider not in _IMAGE_DEFAULTS:
        raise ValueError(
            f"image capability not implemented for provider {row.provider!r}",
        )
    base_url, model, provider = _IMAGE_DEFAULTS[row.provider]
    base_url = _config_str(row, "base_url", base_url)
    model = _config_str(
        row,
        "image_model",
        _config_str(row, "default_model", model),
    )
    if not base_url or not model:
        raise ValueError("image provider requires base_url and image_model")
    return ImageProfile(
        id=row.provider,
        label=row.label,
        kind="external_api",
        api=ExternalImageApiProfileConfig(
            base_url=base_url,
            api_key=_optional_secret(secret, "api_key") or "",
            model=model,
            provider=_config_str(row, "provider", provider),
            timeout_seconds=float(_config_int(row, "timeout_seconds", 180)),
        ),
    )


def build_comfyui_profile(row: ProviderConnection) -> ImageProfile:
    """Build a kind=comfyui ImageProfile from a `comfyui` connection row.

    The (server, checkpoint, workflow_file) tuple lives entirely in
    ``config`` — no secret. A blank ``server`` is a config error the
    caller records as a runtime status (the profile registry would also
    degrade a server-less comfyui profile to ``None`` at build time, but
    surfacing it here gives the operator a clear message)."""
    server = _config_str(row, "server", "")
    if not server:
        raise ValueError("ComfyUI provider requires server")
    return ImageProfile(
        id=row.provider,
        label=row.label,
        kind="comfyui",
        comfyui=ComfyProfileConfig(
            server=server,
            checkpoint=_config_str(row, "checkpoint", ""),
            workflow_file=_config_str(row, "workflow_file", ""),
            generation_timeout_seconds=float(
                _config_int(row, "timeout_seconds", 180),
            ),
        ),
    )


def build_image_provider(
    row: ProviderConnection,
    secret: dict[str, Any],
):
    """Build the runtime image provider for an ``image`` row (probe use).

    Reuses BOTH existing mappings — row→profile (above) and
    profile→provider (the runtime ``ImageProfileRegistry`` dispatch) —
    so the probe exercises the exact adapter the runtime would build.
    Returns ``None`` for providers with no external-API image wiring
    (incl. ComfyUI, whose probe stays a reachability check).
    """
    if row.provider == "comfyui" or row.provider not in _IMAGE_DEFAULTS:
        return None
    profile = build_image_profile(row, secret)
    # Lazy import: the registry pulls in the ComfyUI generator stack,
    # which live_probe (imported at service init) shouldn't pay for
    # until a deep image probe actually runs.
    from kokoro_link.infrastructure.image.profile_registry import (
        ImageProfileRegistry,
    )

    return ImageProfileRegistry([profile]).resolve(profile.id)


# ---------------------------------------------------------------------------
# video
# ---------------------------------------------------------------------------


def build_video_profile(
    row: ProviderConnection,
    secret: dict[str, Any],
) -> VideoProfile:
    """Build the VideoProfile for a ``video`` row.

    Raises ``ValueError`` for unsupported providers or missing config."""
    if row.provider not in _VIDEO_DEFAULTS:
        raise ValueError(
            f"video capability not implemented for provider {row.provider!r}",
        )
    base_url, model, provider = _VIDEO_DEFAULTS[row.provider]
    base_url = _config_str(row, "base_url", base_url)
    model = _config_str(row, "default_model", model)
    if not base_url or not model:
        raise ValueError("video provider requires base_url and default_model")
    return VideoProfile(
        id=row.provider,
        label=row.label,
        kind="external_api",
        api=ExternalVideoApiProfileConfig(
            base_url=base_url,
            api_key=_optional_secret(secret, "api_key") or "",
            model=model,
            provider=_config_str(row, "provider", provider),
            timeout_seconds=float(_config_int(row, "timeout_seconds", 1800)),
        ),
    )
