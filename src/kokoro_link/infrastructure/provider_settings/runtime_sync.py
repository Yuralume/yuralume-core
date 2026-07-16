"""Synchronise persisted BYOK provider settings into runtime registries."""

from __future__ import annotations

import json
import logging
from typing import Any

from kokoro_link.bootstrap.container import ServiceContainer
from kokoro_link.bootstrap.settings import AppSettings, TTSSettings
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
from kokoro_link.infrastructure.embedder.null import NullEmbedder
from kokoro_link.infrastructure.llm.anthropic import AnthropicChatModel
from kokoro_link.infrastructure.llm.openai_compatible import OpenAICompatibleChatModel
from kokoro_link.infrastructure.persistence.models import MEMORY_EMBEDDING_DIM
from kokoro_link.infrastructure.tts.external_api import (
    ExternalTTSAdapter,
    OpenAITTSAdapter,
)
from kokoro_link.infrastructure.tools.websearch import (
    DuckDuckGoSearchClient,
    OpenAIWebSearchClient,
    SearchClientPort,
    SearXNGSearchClient,
    TavilyClient,
    WebSearchTool,
)
from kokoro_link.infrastructure.provider_settings.catalog import catalog_by_id
from kokoro_link.infrastructure.security.provider_secret_cipher import (
    ProviderSecretCipherError,
)

_LOGGER = logging.getLogger(__name__)

# Providers whose /audio/speech endpoint speaks the OpenAI speech
# protocol (so they ride OpenAITTSAdapter rather than the generic
# gateway ExternalTTSAdapter). This is a protocol-compatibility set,
# not a per-provider hardcode: OpenRouter proxies OpenAI's speech
# endpoint verbatim (verified 2026-07-05).
_OPENAI_SPEECH_PROTOCOL_PROVIDERS = frozenset({"openai", "openrouter"})

_OPENAI_COMPATIBLE_DEFAULTS: dict[str, tuple[str, str]] = {
    "openai": ("https://api.openai.com/v1", "gpt-4o-mini"),
    "google_gemini": (
        "https://generativelanguage.googleapis.com/v1beta/openai",
        "gemini-2.0-flash",
    ),
    "openrouter": ("https://openrouter.ai/api/v1", "openai/gpt-4o-mini"),
    "nanogpt": ("https://nano-gpt.com/api/v1", "gpt-5.2"),
    "deepseek": ("https://api.deepseek.com/v1", "deepseek-chat"),
    "mistral": ("https://api.mistral.ai/v1", "mistral-small-latest"),
    "custom_openai_compatible": ("", ""),
    "local_openai_compatible": ("http://127.0.0.1:1234/v1", "local-model"),
    "yuralume_cloud": ("", ""),
}


def default_base_url_for(provider_id: str) -> str:
    """Best-known API base for a catalog provider.

    Model discovery uses this when a draft connection omits ``base_url``,
    mirroring the runtime adapters above which apply the same default at
    sync time — so "leave the field empty for the provider default" holds
    for both saving and the fetch-models probe. Providers without a known
    base (custom / yuralume_cloud) return ``""`` and keep the explicit
    "base_url is required" discovery error.
    """
    defaults = _OPENAI_COMPATIBLE_DEFAULTS.get(provider_id)
    return defaults[0] if defaults else ""


_IMAGE_DEFAULTS: dict[str, tuple[str, str, str]] = {
    "openai": ("https://api.openai.com/v1", "gpt-image-2", "openai"),
    "google_gemini": (
        "https://generativelanguage.googleapis.com/v1beta",
        "gemini-2.5-flash-image",
        "gemini",
    ),
    "xai": ("https://api.x.ai/v1", "grok-2-image-1212", "xai"),
    # OpenRouter posts /api/v1/images (not /images/generations) → its own
    # provider kind routes to OpenRouterImageProvider (verified 2026-07-05).
    "openrouter": ("https://openrouter.ai/api/v1", "black-forest-labs/flux.2-pro", "openrouter"),
    # NanoGPT's OpenAI-compatible /v1/images/generations returns b64_json →
    # gateway kind rides ExternalImageApiProvider (verified 2026-07-05).
    "nanogpt": ("https://nano-gpt.com/api/v1", "flux-1.1-pro", "gateway"),
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

_TTS_DEFAULTS: dict[str, tuple[str, str, str]] = {
    "openai": ("https://api.openai.com/v1", "gpt-4o-mini-tts", "marin"),
    "openrouter": ("https://openrouter.ai/api/v1", "openai/gpt-4o-mini-tts", "alloy"),
    "custom_tts": ("", "", ""),
    "yuralume_cloud": ("", "", ""),
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


async def seed_legacy_provider_connections(
    container: ServiceContainer,
    settings: AppSettings,
) -> None:
    """One-time seed from deprecated provider env into DB-backed settings.

    The compatibility path only runs when there are no provider rows at
    all. After the first Admin UI write, DB state is the source of truth.
    """
    service = getattr(container, "provider_connection_service", None)
    if service is None:
        return
    if await service.list_connections():
        return
    for draft in _legacy_provider_drafts(settings):
        try:
            await service.create_connection(**draft)
        except Exception as exc:
            _LOGGER.warning(
                "legacy provider env seed skipped %s: %s",
                draft.get("provider"),
                exc,
            )


async def sync_provider_connections(container: ServiceContainer) -> None:
    """Register enabled DB-backed providers into mutable registries.

    The registry contract itself is intentionally small, but the
    concrete in-memory registry used by the app exposes ``register`` /
    ``unregister``. We feature-detect those methods so tests using
    stubs still work.
    """
    service = getattr(container, "provider_connection_service", None)
    registry = getattr(container, "model_registry", None)
    register = getattr(registry, "register", None)
    unregister = getattr(registry, "unregister", None)
    if service is None or registry is None or register is None:
        return

    catalog = catalog_by_id()
    all_rows = await service.list_connections()
    configured_llm_provider_ids: set[str] = set()
    for row in all_rows:
        entry = catalog.get(row.provider)
        if entry is not None and "llm" in row.capabilities and "llm" in entry.capabilities:
            configured_llm_provider_ids.add(row.provider)
    if unregister is not None:
        for provider_id in configured_llm_provider_ids:
            unregister(provider_id)

    rows = await service.list_enabled_runtime(capability="llm")
    for row in rows:
        try:
            model = await _build_llm_model(container, row)
        except Exception as exc:
            _LOGGER.warning(
                "provider settings sync skipped %s (%s): %s",
                row.id,
                row.provider,
                exc,
            )
            await service.record_runtime_status(row.id, error=str(exc))
            continue
        if model is not None:
            register(model)
            await service.record_runtime_status(row.id, error=None)
    container.provider_ids = registry.list_ids()

    await _sync_image_profiles(container)
    await _sync_video_profiles(container)
    await _sync_tts_backend(container)
    await _sync_embedding_backend(container)
    await _sync_search_tool(container)


def _legacy_provider_drafts(settings: AppSettings) -> list[dict[str, Any]]:
    drafts: list[dict[str, Any]] = []
    llm_provider_map = {"gemini": "google_gemini", "lmstudio": "local_openai_compatible"}
    for provider in settings.openai_compatible_providers:
        legacy_provider_id = str(provider.get("provider_id") or "")
        provider_id = llm_provider_map.get(legacy_provider_id, legacy_provider_id)
        api_key = provider.get("api_key")
        secret = (
            {"api_key": api_key}
            if provider_id != "local_openai_compatible" and api_key
            else {}
        )
        drafts.append(
            {
                "provider": provider_id,
                "label": f"{provider_id} legacy LLM",
                "enabled": True,
                "capabilities": ["llm"],
                "config": {
                    "base_url": provider.get("base_url"),
                    "default_model": provider.get("model"),
                    # Omit the key entirely when the source is None so the
                    # row doesn't seed a literal ``None`` into config —
                    # absent lets the openai_compatible False default apply
                    # (matches the settings.py env path), while a literal
                    # None would be an ambiguous stored value.
                    **(
                        {"supports_vision": provider.get("supports_vision")}
                        if provider.get("supports_vision") is not None
                        else {}
                    ),
                    **(
                        {"max_tokens": provider.get("max_tokens")}
                        if provider.get("max_tokens") is not None
                        else {}
                    ),
                },
                "secret": secret,
            },
        )
    if settings.anthropic.enabled:
        drafts.append(
            {
                "provider": "anthropic",
                "label": "Anthropic legacy LLM",
                "enabled": True,
                "capabilities": ["llm"],
                "config": {
                    "base_url": settings.anthropic.base_url,
                    "default_model": settings.anthropic.model,
                    "anthropic_version": settings.anthropic.anthropic_version,
                    "supports_vision": settings.anthropic.supports_vision,
                    "max_tokens": settings.anthropic.max_tokens,
                },
                "secret": {"api_key": settings.anthropic.api_key},
            },
        )
    if settings.image_api.enabled:
        provider_id = _legacy_media_provider_id(
            settings.image_api.provider,
            media_kind="image",
        )
        drafts.append(
            {
                "provider": provider_id,
                "label": f"{provider_id} legacy image",
                "enabled": True,
                "capabilities": ["image"],
                "config": {
                    "base_url": settings.image_api.base_url,
                    "default_model": settings.image_api.model,
                    "timeout_seconds": settings.image_api.timeout_seconds,
                },
                "secret": {"api_key": settings.image_api.api_key},
            },
        )
    if settings.comfyui.enabled:
        # KOKORO_COMFYUI_* → a kind=comfyui image row. lora_dir is a
        # deploy path used only by the LoRA upload endpoint (not part of
        # ComfyProfileConfig), but we persist it so the operator can see /
        # edit it from the same admin row.
        drafts.append(
            {
                "provider": "comfyui",
                "label": "ComfyUI legacy image",
                "enabled": True,
                "capabilities": ["image"],
                "config": {
                    "server": settings.comfyui.server,
                    "checkpoint": settings.comfyui.checkpoint,
                    "workflow_file": settings.comfyui.workflow_file,
                    "lora_dir": settings.comfyui.lora_dir,
                    "timeout_seconds": settings.comfyui.generation_timeout_seconds,
                },
                "secret": {},
            },
        )
    if settings.video_api.enabled:
        provider_id = _legacy_media_provider_id(
            settings.video_api.provider,
            media_kind="video",
        )
        drafts.append(
            {
                "provider": provider_id,
                "label": f"{provider_id} legacy video",
                "enabled": True,
                "capabilities": ["video"],
                "config": {
                    "base_url": settings.video_api.base_url,
                    "default_model": settings.video_api.model,
                    "timeout_seconds": settings.video_api.timeout_seconds,
                },
                "secret": {"api_key": settings.video_api.api_key},
            },
        )
    if settings.tts.enabled:
        provider_id = "openai" if settings.tts.provider == "openai" else "custom_tts"
        tts_config: dict[str, Any] = {
            "base_url": settings.tts.base_url,
            "default_model": settings.tts.model,
            "voice_id": settings.tts.voice_id,
            "timeout_seconds": settings.tts.timeout_seconds,
        }
        # response_format only belongs to the OpenAI speech protocol
        # (OpenAITTSAdapter reads it). The custom_tts catalog exposes no such
        # field and ExternalTTSAdapter ignores it, so seeding it there would
        # make create_connection reject the whole draft and drop the TTS row.
        if provider_id == "openai":
            tts_config["response_format"] = settings.tts.response_format
        drafts.append(
            {
                "provider": provider_id,
                "label": f"{provider_id} legacy TTS",
                "enabled": True,
                "capabilities": ["tts"],
                "config": tts_config,
                "secret": {"api_key": settings.tts.api_key}
                if settings.tts.api_key
                else {},
            },
        )
    if settings.tavily.enabled:
        drafts.append(
            {
                "provider": "tavily",
                "label": "Tavily legacy search",
                "enabled": True,
                "capabilities": ["search"],
                # tavily catalog exposes no base_url field (endpoint is
                # fixed); the client defaults to https://api.tavily.com.
                "config": {
                    "search_depth": settings.tavily.search_depth,
                    "max_results": settings.tavily.max_results,
                    "timeout_seconds": settings.tavily.timeout_seconds,
                },
                "secret": {"api_key": settings.tavily.api_key},
            },
        )
    if settings.use_embedder:
        drafts.append(
            {
                "provider": "local_openai_compatible",
                "label": "Legacy embedding",
                "enabled": True,
                "capabilities": ["embedding"],
                "config": {
                    "base_url": settings.embedding.base_url,
                    "embedding_model": settings.embedding.model,
                    "embedding_dimension": settings.embedding.dimension,
                    "request_dimensions": False,
                },
                "secret": {"api_key": settings.embedding.api_key}
                if settings.embedding.api_key
                else {},
            },
        )
    return drafts


def _legacy_media_provider_id(provider: str, *, media_kind: str) -> str:
    normalized = provider.strip().lower()
    if normalized in {"google_gemini", "gemini"}:
        return "google_gemini"
    if normalized in {"xai", "openai"}:
        return normalized
    if normalized in {"google_veo", "veo"} and media_kind == "video":
        return "google_veo"
    return "custom_media_gateway"


async def _build_llm_model(
    container: ServiceContainer,
    row: ProviderConnection,
):
    service = container.provider_connection_service
    if service is None:
        return None
    secret: dict[str, Any]
    try:
        secret = await service.get_decrypted_secret(row.id)
    except ProviderSecretCipherError as exc:
        raise ValueError("stored secret cannot be decrypted") from exc
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


async def _sync_image_profiles(container: ServiceContainer) -> None:
    service = container.provider_connection_service
    registry = getattr(container, "image_profile_registry", None)
    replace_profiles = getattr(registry, "replace_profiles", None)
    if service is None or replace_profiles is None:
        return
    all_rows = await service.list_connections()
    if not any("image" in row.capabilities for row in all_rows):
        return
    rows = await service.list_enabled_runtime(capability="image")
    profiles: list[ImageProfile] = []
    for row in rows:
        if row.provider == "comfyui":
            # ComfyUI direct-connect → kind=comfyui profile. No secret; the
            # (server, checkpoint, workflow) tuple lives entirely in config.
            try:
                profiles.append(_build_comfyui_profile(row))
                await service.record_runtime_status(row.id, error=None)
            except Exception as exc:
                _LOGGER.warning(
                    "provider settings image sync skipped %s (%s): %s",
                    row.id,
                    row.provider,
                    exc,
                )
                await service.record_runtime_status(row.id, error=str(exc))
            continue
        if row.provider not in _IMAGE_DEFAULTS:
            await service.record_runtime_status(
                row.id,
                error=f"image capability not implemented for provider {row.provider!r}",
            )
            continue
        try:
            secret = await service.get_decrypted_secret(row.id)
            base_url, model, provider = _IMAGE_DEFAULTS[row.provider]
            base_url = _config_str(row, "base_url", base_url)
            model = _config_str(
                row,
                "image_model",
                _config_str(row, "default_model", model),
            )
            if not base_url or not model:
                raise ValueError("image provider requires base_url and image_model")
            profiles.append(
                ImageProfile(
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
                ),
            )
            await service.record_runtime_status(row.id, error=None)
        except Exception as exc:
            _LOGGER.warning(
                "provider settings image sync skipped %s (%s): %s",
                row.id,
                row.provider,
                exc,
            )
            await service.record_runtime_status(row.id, error=str(exc))
    replace_profiles(profiles)


def _build_comfyui_profile(row: ProviderConnection) -> ImageProfile:
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


async def _sync_video_profiles(container: ServiceContainer) -> None:
    service = container.provider_connection_service
    registry = getattr(container, "video_profile_registry", None)
    replace_profiles = getattr(registry, "replace_profiles", None)
    if service is None or replace_profiles is None:
        return
    all_rows = await service.list_connections()
    if not any("video" in row.capabilities for row in all_rows):
        return
    rows = await service.list_enabled_runtime(capability="video")
    profiles: list[VideoProfile] = []
    for row in rows:
        if row.provider not in _VIDEO_DEFAULTS:
            await service.record_runtime_status(
                row.id,
                error=f"video capability not implemented for provider {row.provider!r}",
            )
            continue
        try:
            secret = await service.get_decrypted_secret(row.id)
            base_url, model, provider = _VIDEO_DEFAULTS[row.provider]
            base_url = _config_str(row, "base_url", base_url)
            model = _config_str(row, "default_model", model)
            if not base_url or not model:
                raise ValueError("video provider requires base_url and default_model")
            profiles.append(
                VideoProfile(
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
                ),
            )
            await service.record_runtime_status(row.id, error=None)
        except Exception as exc:
            _LOGGER.warning(
                "provider settings video sync skipped %s (%s): %s",
                row.id,
                row.provider,
                exc,
            )
            await service.record_runtime_status(row.id, error=str(exc))
    replace_profiles(profiles)


async def _sync_tts_backend(container: ServiceContainer) -> None:
    service = container.provider_connection_service
    tts_service = getattr(container, "tts_service", None)
    if service is None or tts_service is None:
        return
    all_rows = await service.list_connections()
    if not any("tts" in row.capabilities for row in all_rows):
        return
    rows = await service.list_enabled_runtime(capability="tts")
    if not rows:
        container.tts_voice_catalog = None
        return
    row = max(rows, key=_runtime_updated_at)
    if row.provider not in _TTS_DEFAULTS:
        return
    try:
        secret = await service.get_decrypted_secret(row.id)
        default_base_url, default_model, default_voice = _TTS_DEFAULTS[row.provider]
        base_url = _config_str(row, "base_url", default_base_url)
        model = _config_str(
            row,
            "tts_model",
            _config_str(row, "default_model", default_model),
        )
        voice_id = _config_str(row, "voice_id", default_voice)
        api_key = _optional_secret(secret, "api_key") or ""
        timeout = float(_config_int(row, "timeout_seconds", 90))
        if row.provider in _OPENAI_SPEECH_PROTOCOL_PROVIDERS:
            port = OpenAITTSAdapter(
                api_key=api_key,
                base_url=base_url,
                model=model or "gpt-4o-mini-tts",
                default_voice_id=voice_id or "marin",
                response_format=_config_str(row, "response_format", "wav"),
                timeout_seconds=timeout,
            )
            settings = TTSSettings(
                provider="openai",
                base_url=base_url,
                api_key=api_key,
                model=model,
                voice_id=voice_id,
                response_format=_config_str(row, "response_format", "wav"),
                timeout_seconds=timeout,
            )
        else:
            if not base_url:
                raise ValueError("custom TTS provider requires base_url")
            port = ExternalTTSAdapter(
                base_url=base_url,
                api_key=api_key,
                default_voice_id=voice_id,
                timeout_seconds=timeout,
            )
            settings = TTSSettings(
                provider="custom",
                base_url=base_url,
                api_key=api_key,
                model=model,
                voice_id=voice_id,
                timeout_seconds=timeout,
            )
        tts_service.set_runtime_backend(port=port, settings=settings)
        container.tts_voice_catalog = port
        await service.record_runtime_status(row.id, error=None)
    except Exception as exc:
        _LOGGER.warning(
            "provider settings TTS sync skipped %s (%s): %s",
            row.id,
            row.provider,
            exc,
        )
        await service.record_runtime_status(row.id, error=str(exc))


async def _sync_embedding_backend(container: ServiceContainer) -> None:
    service = container.provider_connection_service
    embedder = getattr(container, "embedder", None)
    set_backend = getattr(embedder, "set_backend", None)
    if service is None or set_backend is None:
        return
    all_rows = await service.list_connections()
    if not any("embedding" in row.capabilities for row in all_rows):
        return
    rows = await service.list_enabled_runtime(capability="embedding")
    if not rows:
        set_backend(NullEmbedder(dimension=embedder.dimension))
        return
    row = max(rows, key=_runtime_updated_at)
    if row.provider not in _EMBEDDING_DEFAULTS:
        return
    try:
        secret = await service.get_decrypted_secret(row.id)
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
        set_backend(
            LMStudioEmbedder(
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
            ),
        )
        await service.record_runtime_status(row.id, error=None)
    except Exception as exc:
        _LOGGER.warning(
            "provider settings embedding sync skipped %s (%s): %s",
            row.id,
            row.provider,
            exc,
        )
        await service.record_runtime_status(row.id, error=str(exc))


async def _sync_search_tool(container: ServiceContainer) -> None:
    """Hot-(un)wire the ``web_search`` ToolPort from DB `search` rows.

    Single-active-row semantics (mirrors ``_sync_tts_backend``): the tool
    name ``web_search`` is globally unique, so at most one search backend
    can be mounted. When several enabled rows exist the most-recently
    updated one wins.

    Feature-detects ``replace`` / ``unregister`` on the tool registry via
    ``getattr`` so stub registries in tests (which need neither) are
    skipped rather than crashing — same pattern as the LLM registry
    branch in ``sync_provider_connections``.
    """
    service = container.provider_connection_service
    registry = getattr(container, "tool_registry", None)
    replace = getattr(registry, "replace", None)
    unregister = getattr(registry, "unregister", None)
    if service is None or registry is None or replace is None or unregister is None:
        return
    all_rows = await service.list_connections()
    if not any("search" in row.capabilities for row in all_rows):
        # No search rows at all → leave any env-wired tool in place.
        return
    rows = await service.list_enabled_runtime(capability="search")
    if not rows:
        unregister("web_search")
        return
    row = max(rows, key=_runtime_updated_at)
    if row.provider not in _SEARCH_PROVIDERS:
        await service.record_runtime_status(
            row.id,
            error=f"search capability not implemented for provider {row.provider!r}",
        )
        return
    try:
        secret = await service.get_decrypted_secret(row.id)
        client = _build_search_client(row, secret)
        # Build the tool fully before ``replace`` so the chat loop never
        # observes a half-wired ``web_search`` (see plan risk note).
        tool = WebSearchTool(
            client=client,
            default_max_results=_config_int(row, "max_results", 5),
        )
        replace(tool)
        await service.record_runtime_status(row.id, error=None)
    except Exception as exc:
        _LOGGER.warning(
            "provider settings search sync skipped %s (%s): %s",
            row.id,
            row.provider,
            exc,
        )
        await service.record_runtime_status(row.id, error=str(exc))


def _build_search_client(
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


def _runtime_updated_at(row: ProviderConnection) -> str:
    stamp = row.updated_at or row.created_at
    return stamp.isoformat() if stamp is not None else ""
