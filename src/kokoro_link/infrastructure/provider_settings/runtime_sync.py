"""Synchronise persisted BYOK provider settings into runtime registries.

The row→adapter mapping itself lives in :mod:`adapter_builders` (single
source of truth shared with the live probe); this module owns the
*wiring*: decrypting secrets, registering adapters into the mutable
registries, recording runtime statuses, and the one-time legacy env
seed. Provider defaults are re-exported below for backwards
compatibility (several routes/tests import them from here).
"""

from __future__ import annotations

import logging
from typing import Any

from kokoro_link.bootstrap.container import ServiceContainer
from kokoro_link.bootstrap.settings import AppSettings, TTSSettings
from kokoro_link.contracts.image_profile import ImageProfile
from kokoro_link.contracts.provider_settings import ProviderConnection
from kokoro_link.contracts.video_profile import VideoProfile
from kokoro_link.infrastructure.embedder.null import NullEmbedder
from kokoro_link.infrastructure.tools.websearch import WebSearchTool
from kokoro_link.infrastructure.provider_settings import adapter_builders
from kokoro_link.infrastructure.provider_settings.adapter_builders import (  # noqa: F401 — re-exported for routes/tests
    _EMBEDDING_DEFAULTS,
    _IMAGE_DEFAULTS,
    _OPENAI_COMPATIBLE_DEFAULTS,
    _OPENAI_SPEECH_PROTOCOL_ADAPTERS,
    _OPENAI_SPEECH_PROTOCOL_PROVIDERS,
    _SEARCH_PROVIDERS,
    _TTS_DEFAULTS,
    _VIDEO_DEFAULTS,
    _config_bool,
    _config_int,
    _config_optional_int,
    _config_optional_json_object,
    _config_optional_str,
    _config_str,
    _optional_secret,
    default_base_url_for,
)
from kokoro_link.infrastructure.provider_settings.adapter_builders import (
    build_search_client as _build_search_client,  # noqa: F401 — legacy name kept for tests
)
from kokoro_link.infrastructure.provider_settings.catalog import catalog_by_id
from kokoro_link.infrastructure.security.provider_secret_cipher import (
    ProviderSecretCipherError,
)

_LOGGER = logging.getLogger(__name__)


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
    # Row→adapter mapping lives in adapter_builders (shared with the
    # live probe) so probe and runtime can never diverge.
    return adapter_builders.build_chat_model(row, secret)


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
                profiles.append(adapter_builders.build_comfyui_profile(row))
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
        try:
            secret = await service.get_decrypted_secret(row.id)
            profiles.append(adapter_builders.build_image_profile(row, secret))
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
        try:
            secret = await service.get_decrypted_secret(row.id)
            profiles.append(adapter_builders.build_video_profile(row, secret))
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
        built = adapter_builders.build_tts(row, secret)
        if built is None:
            return
        if built.provider_kind == "openai":
            settings = TTSSettings(
                provider="openai",
                base_url=built.base_url,
                api_key=built.api_key,
                model=built.model,
                voice_id=built.voice_id,
                response_format=built.response_format,
                timeout_seconds=built.timeout_seconds,
            )
        else:
            settings = TTSSettings(
                provider="custom",
                base_url=built.base_url,
                api_key=built.api_key,
                model=built.model,
                voice_id=built.voice_id,
                timeout_seconds=built.timeout_seconds,
            )
        tts_service.set_runtime_backend(port=built.port, settings=settings)
        container.tts_voice_catalog = built.port
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
        backend = adapter_builders.build_embedder(row, secret)
        if backend is None:
            return
        set_backend(backend)
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


def _runtime_updated_at(row: ProviderConnection) -> str:
    stamp = row.updated_at or row.created_at
    return stamp.isoformat() if stamp is not None else ""
