"""Live capability probes for BYOK provider connections.

``probe_connection`` upgrades the admin "Test" button from pure local
validation to real network checks. Stateless like
:mod:`model_discovery` in this package: probes are built from the draft
config/secret (or a decrypted stored row) and speak httpx directly — no
runtime adapter or registry state is touched, so a probe can never
disturb the live wiring.

Shared API contract (mirrored by the admin frontend):

* One :class:`ProbeReport` per performed check; ``action`` is pinned to
  the shared enum (``listed_models`` / ``chat_completion`` /
  ``embedded`` / ``listed_voices`` / ``synthesized_speech`` /
  ``searched`` / ``reachability`` / ``generated_image`` /
  ``not_probed``; the service layer additionally emits ``config_check``
  for local-validation failures before any network is touched).
* Fail-soft: one capability failing never aborts the others, and a
  probe never raises — failures become failed reports.
* A (provider, capability) pair with no probe strategy yields an honest
  ``not_probed`` with ``ok=True`` so the absence of a probe can never
  fail the connection card.
* Every detail string passes :func:`sanitize_error` so probe output can
  never echo an API key.
"""

from __future__ import annotations

import base64
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import httpx

from kokoro_link.contracts.provider_settings import ProviderConnection
from kokoro_link.infrastructure.persistence.models import MEMORY_EMBEDDING_DIM
from kokoro_link.infrastructure.provider_settings.catalog import ProviderCatalogEntry
from kokoro_link.infrastructure.provider_settings.model_discovery import (
    _OPENAI_COMPATIBLE_ADAPTERS,
    _bearer_headers,
    _extract_openai_model_ids,
    _parse_gateway_options,
    gateway_options_url,
)
from kokoro_link.infrastructure.security.error_sanitizer import (
    redact_values,
    sanitize_error,
)

DEFAULT_TIMEOUT_SECONDS = 15.0
REACHABILITY_TIMEOUT_SECONDS = 8.0
# Matches the runtime image adapters' default (runtime_sync: 180s) and the
# published gateway spec — a compliant gateway that generates in 121-180s
# must not fail the probe while passing at runtime.
DEEP_IMAGE_TIMEOUT_SECONDS = 180.0

_CHAT_PROBE_TEXT = "ping"
_SEARCH_PROBE_QUERY = "ping"
_TTS_PROBE_TEXT = "Hi"
_IMAGE_PROBE_PROMPT = "a tiny plain blue circle"

_GATEWAY_REACHABLE_DETAIL = (
    "endpoint reachable — generation not verified (run deep test)"
)
_VIDEO_REACHABLE_DETAIL = (
    "endpoint reachable — deep video validation is intentionally "
    "unsupported (generation takes minutes)"
)


@dataclass(frozen=True, slots=True)
class ProbeReport:
    """One live check on one capability — mirrors the shared API contract."""

    capability: str
    action: str
    ok: bool
    detail: str
    latency_ms: int


_CheckResult = tuple[bool, str]
_Check = Callable[[], Awaitable[_CheckResult]]


def _runtime():
    """Lazy import of :mod:`runtime_sync` (shared defaults + client builders).

    ``runtime_sync`` imports ``bootstrap.container`` which imports the
    provider connection service, which imports this module — a
    module-level import here would close that cycle and crash at boot.
    By the time a probe actually runs, everything is fully loaded.
    """
    from kokoro_link.infrastructure.provider_settings import runtime_sync

    return runtime_sync


async def probe_connection(
    *,
    entry: ProviderCatalogEntry,
    capabilities: Sequence[str],
    config: dict[str, Any],
    secret: dict[str, Any],
    deep: bool = False,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[ProbeReport]:
    """Run one live probe pass per requested capability.

    ``transport`` is a test seam forwarded into every ``httpx`` client
    this module builds (the ``search`` branch is the one exception — the
    websearch clients own their HTTP stack, so tests patch
    ``httpx.AsyncClient`` for that branch instead).
    """
    reports: list[ProbeReport] = []
    for capability in capabilities:
        handler = _CAPABILITY_PROBES.get(capability)
        if handler is None:
            reports.append(
                _not_probed(
                    capability,
                    f"no live probe implemented for capability {capability!r}",
                ),
            )
            continue
        try:
            reports.extend(
                await handler(
                    entry,
                    dict(config),
                    dict(secret),
                    deep=deep,
                    transport=transport,
                ),
            )
        except Exception as exc:  # fail-soft: one capability never aborts the rest
            reports.append(
                ProbeReport(
                    capability=capability,
                    action=_FALLBACK_ACTIONS.get(capability, "not_probed"),
                    ok=False,
                    detail=sanitize_error(str(exc) or exc.__class__.__name__),
                    latency_ms=0,
                ),
            )
    # Final choke point: pattern scrubbing runs per probe, but details may
    # embed provider response snippets echoing arbitrarily-shaped secrets —
    # redact the exact values we know were sent.
    known_secrets = tuple(v for v in secret.values() if isinstance(v, str))
    if known_secrets:
        reports = [
            ProbeReport(
                capability=r.capability,
                action=r.action,
                ok=r.ok,
                detail=redact_values(r.detail, known_secrets),
                latency_ms=r.latency_ms,
            )
            for r in reports
        ]
    return reports


# ---------------------------------------------------------------------------
# Probe plumbing
# ---------------------------------------------------------------------------


def _client(
    timeout_seconds: float,
    transport: httpx.AsyncBaseTransport | None,
) -> httpx.AsyncClient:
    kwargs: dict[str, Any] = {"timeout": timeout_seconds}
    if transport is not None:
        kwargs["transport"] = transport
    return httpx.AsyncClient(**kwargs)


async def _run(capability: str, action: str, check: _Check) -> ProbeReport:
    start = time.perf_counter()
    try:
        ok, detail = await check()
    except httpx.TimeoutException:
        ok, detail = False, "request timed out"
    except httpx.HTTPError as exc:
        ok, detail = False, f"connection failed: {exc}"
    except Exception as exc:  # a probe must never raise
        ok, detail = False, str(exc) or exc.__class__.__name__
    latency_ms = int((time.perf_counter() - start) * 1000)
    return ProbeReport(
        capability=capability,
        action=action,
        ok=ok,
        detail=sanitize_error(detail),
        latency_ms=latency_ms,
    )


def _not_probed(capability: str, detail: str) -> ProbeReport:
    return ProbeReport(
        capability=capability,
        action="not_probed",
        ok=True,
        detail=sanitize_error(detail),
        latency_ms=0,
    )


def _cfg_str(config: dict[str, Any], key: str, default: str = "") -> str:
    value = config.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return default


def _cfg_float(config: dict[str, Any], key: str, default: float) -> float:
    value = config.get(key)
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _cfg_bool(config: dict[str, Any], key: str, default: bool = False) -> bool:
    value = config.get(key)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return default


def _http_error_detail(response: httpx.Response) -> str:
    detail = f"HTTP {response.status_code} {response.reason_phrase}".strip()
    snippet = (response.text or "").strip().replace("\n", " ")[:160]
    return f"{detail}: {snippet}" if snippet else detail


def _safe_json(response: httpx.Response) -> Any | None:
    try:
        return response.json()
    except ValueError:
        return None


async def _reachable(
    base_url: str,
    transport: httpx.AsyncBaseTransport | None,
    *,
    ok_detail: str,
) -> _CheckResult:
    """ANY HTTP response (even 404/405) proves the endpoint resolves and
    answers; only DNS/connect/timeout errors fail (raised → mapped by
    ``_run``)."""
    if not base_url:
        return False, "base_url is required for a reachability check"
    async with _client(REACHABILITY_TIMEOUT_SECONDS, transport) as client:
        response = await client.get(base_url)
    return True, f"{ok_detail} (HTTP {response.status_code})"


# ---------------------------------------------------------------------------
# llm
# ---------------------------------------------------------------------------


async def _probe_llm(
    entry: ProviderCatalogEntry,
    config: dict[str, Any],
    secret: dict[str, Any],
    *,
    deep: bool,
    transport: httpx.AsyncBaseTransport | None,
) -> list[ProbeReport]:
    del deep  # llm probing is identical in both modes
    api_key = _cfg_str(secret, "api_key")
    if entry.id == "yuralume_cloud":
        base = _cfg_str(config, "base_url").rstrip("/")
        return [
            await _run(
                "llm",
                "listed_models",
                lambda: _gateway_listed_models(base, api_key, "llm", transport),
            ),
        ]
    if entry.adapter_kind == "anthropic":
        return [
            await _run(
                "llm",
                "chat_completion",
                lambda: _anthropic_chat(config, api_key, transport),
            ),
        ]
    runtime = _runtime()
    defaults = runtime._OPENAI_COMPATIBLE_DEFAULTS.get(entry.id)
    if entry.adapter_kind not in _OPENAI_COMPATIBLE_ADAPTERS and defaults is None:
        return [_not_probed("llm", f"no live LLM probe for provider {entry.id!r}")]
    base = (
        _cfg_str(config, "base_url") or runtime.default_base_url_for(entry.id)
    ).rstrip("/")
    reports = [
        await _run(
            "llm",
            "listed_models",
            lambda: _openai_listed_models(base, api_key, transport),
        ),
    ]
    model = _cfg_str(config, "default_model") or (defaults[1] if defaults else "")
    if model:
        reports.append(
            await _run(
                "llm",
                "chat_completion",
                lambda: _openai_chat(base, api_key, model, transport),
            ),
        )
    return reports


async def _openai_listed_models(
    base_url: str,
    api_key: str,
    transport: httpx.AsyncBaseTransport | None,
) -> _CheckResult:
    if not base_url:
        return False, "base_url is required to list models"
    async with _client(DEFAULT_TIMEOUT_SECONDS, transport) as client:
        response = await client.get(
            f"{base_url}/models",
            headers=_bearer_headers(api_key),
        )
    if response.status_code >= 400:
        return False, _http_error_detail(response)
    payload = _safe_json(response)
    if payload is None:
        return False, "models endpoint returned non-JSON response"
    return True, f"{len(_extract_openai_model_ids(payload))} models"


async def _openai_chat(
    base_url: str,
    api_key: str,
    model: str,
    transport: httpx.AsyncBaseTransport | None,
) -> _CheckResult:
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": _CHAT_PROBE_TEXT}],
        "max_tokens": 1,
    }
    async with _client(DEFAULT_TIMEOUT_SECONDS, transport) as client:
        response = await client.post(
            f"{base_url}/chat/completions",
            headers=_bearer_headers(api_key),
            json=payload,
        )
    if response.status_code >= 400:
        return False, f"model {model!r}: {_http_error_detail(response)}"
    return True, f"model {model!r} completed a 1-token chat"


async def _anthropic_chat(
    config: dict[str, Any],
    api_key: str,
    transport: httpx.AsyncBaseTransport | None,
) -> _CheckResult:
    # Mirror AnthropicChatModel: base has no /v1; tolerate a pasted /v1.
    base = (_cfg_str(config, "base_url") or "https://api.anthropic.com").rstrip("/")
    url = f"{base}/messages" if base.endswith("/v1") else f"{base}/v1/messages"
    model = _cfg_str(config, "default_model") or "claude-sonnet-4-5"
    headers = {
        "x-api-key": api_key,
        "anthropic-version": _cfg_str(config, "anthropic_version") or "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": model,
        "max_tokens": 1,
        "messages": [{"role": "user", "content": _CHAT_PROBE_TEXT}],
    }
    async with _client(DEFAULT_TIMEOUT_SECONDS, transport) as client:
        response = await client.post(url, headers=headers, json=payload)
    if response.status_code >= 400:
        return False, f"model {model!r}: {_http_error_detail(response)}"
    return True, f"model {model!r} completed a 1-token chat"


async def _gateway_listed_models(
    base_url: str,
    api_key: str,
    capability: str,
    transport: httpx.AsyncBaseTransport | None,
) -> _CheckResult:
    if not base_url:
        return False, "base_url is required to list models"
    url = gateway_options_url(base_url, capability)
    if url is None:
        return False, f"gateway exposes no options endpoint for {capability!r}"
    async with _client(DEFAULT_TIMEOUT_SECONDS, transport) as client:
        response = await client.get(url, headers=_bearer_headers(api_key))
    result = _parse_gateway_options(response)
    if result.error:
        return False, result.error
    return True, f"{len(result.models)} models"


# ---------------------------------------------------------------------------
# embedding
# ---------------------------------------------------------------------------


async def _probe_embedding(
    entry: ProviderCatalogEntry,
    config: dict[str, Any],
    secret: dict[str, Any],
    *,
    deep: bool,
    transport: httpx.AsyncBaseTransport | None,
) -> list[ProbeReport]:
    del deep
    runtime = _runtime()
    default_base, default_model, default_request_dims = runtime._EMBEDDING_DEFAULTS.get(
        entry.id,
        ("", "", False),
    )
    base = (_cfg_str(config, "base_url") or default_base).rstrip("/")
    model = (
        _cfg_str(config, "embedding_model")
        or _cfg_str(config, "default_model")
        or default_model
    )
    request_dims = _cfg_bool(config, "request_dimensions", default_request_dims)
    api_key = _cfg_str(secret, "api_key")
    return [
        await _run(
            "embedding",
            "embedded",
            lambda: _embed_ping(base, model, api_key, request_dims, transport),
        ),
    ]


async def _embed_ping(
    base_url: str,
    model: str,
    api_key: str,
    request_dims: bool,
    transport: httpx.AsyncBaseTransport | None,
) -> _CheckResult:
    if not base_url or not model:
        return False, "embedding probe requires base_url and embedding_model"
    payload: dict[str, Any] = {"model": model, "input": [_CHAT_PROBE_TEXT]}
    if request_dims:
        payload["dimensions"] = MEMORY_EMBEDDING_DIM
    async with _client(DEFAULT_TIMEOUT_SECONDS, transport) as client:
        response = await client.post(
            f"{base_url}/embeddings",
            headers=_bearer_headers(api_key),
            json=payload,
        )
    if response.status_code >= 400:
        return False, _http_error_detail(response)
    body = _safe_json(response)
    embedding = None
    if isinstance(body, dict):
        data = body.get("data")
        if isinstance(data, list) and data and isinstance(data[0], dict):
            embedding = data[0].get("embedding")
    if not isinstance(embedding, list):
        return False, "embeddings response carried no vector"
    dimension = len(embedding)
    if dimension != MEMORY_EMBEDDING_DIM:
        # The most common real-world embedding misconfig: a model whose
        # native output can't feed the fixed-width memory store column.
        hint = (
            ""
            if request_dims
            else " — pick a natively matching model or enable Request dimensions"
        )
        return False, (
            f"model {model!r} returned a {dimension}-dim vector but the "
            f"memory store requires {MEMORY_EMBEDDING_DIM}{hint}"
        )
    return True, f"{dimension}-dim vector (matches memory store)"


# ---------------------------------------------------------------------------
# tts
# ---------------------------------------------------------------------------


async def _probe_tts(
    entry: ProviderCatalogEntry,
    config: dict[str, Any],
    secret: dict[str, Any],
    *,
    deep: bool,
    transport: httpx.AsyncBaseTransport | None,
) -> list[ProbeReport]:
    del deep
    runtime = _runtime()
    api_key = _cfg_str(secret, "api_key")
    if entry.id == "yuralume_cloud":
        return [
            _not_probed(
                "tts",
                "gateway TTS is validated at runtime, not from the admin test",
            ),
        ]
    if entry.id in runtime._OPENAI_SPEECH_PROTOCOL_PROVIDERS:
        default_base, default_model, default_voice = runtime._TTS_DEFAULTS.get(
            entry.id,
            ("", "", ""),
        )
        base = (_cfg_str(config, "base_url") or default_base).rstrip("/")
        model = (
            _cfg_str(config, "tts_model")
            or _cfg_str(config, "default_model")
            or default_model
        )
        voice = _cfg_str(config, "voice_id") or default_voice
        response_format = _cfg_str(config, "response_format") or "wav"
        return [
            await _run(
                "tts",
                "synthesized_speech",
                lambda: _openai_speech(
                    base, api_key, model, voice, response_format, transport,
                ),
            ),
        ]
    if entry.adapter_kind == "custom_tts":
        base = _cfg_str(config, "base_url").rstrip("/")
        return [
            await _run(
                "tts",
                "listed_voices",
                lambda: _custom_tts_voices(base, api_key, transport),
            ),
        ]
    return [_not_probed("tts", f"no live TTS probe for provider {entry.id!r}")]


async def _openai_speech(
    base_url: str,
    api_key: str,
    model: str,
    voice: str,
    response_format: str,
    transport: httpx.AsyncBaseTransport | None,
) -> _CheckResult:
    if not base_url:
        return False, "base_url is required to synthesize speech"
    payload = {
        "model": model,
        "voice": voice,
        "input": _TTS_PROBE_TEXT,
        "response_format": response_format,
    }
    async with _client(DEFAULT_TIMEOUT_SECONDS, transport) as client:
        response = await client.post(
            f"{base_url}/audio/speech",
            headers=_bearer_headers(api_key),
            json=payload,
        )
    if response.status_code >= 400:
        return False, _http_error_detail(response)
    return True, f"{len(response.content)} bytes of audio (voice {voice!r})"


async def _custom_tts_voices(
    base_url: str,
    api_key: str,
    transport: httpx.AsyncBaseTransport | None,
) -> _CheckResult:
    if not base_url:
        return False, "base_url is required to list voices"
    async with _client(DEFAULT_TIMEOUT_SECONDS, transport) as client:
        response = await client.get(
            f"{base_url}/voices",
            headers=_bearer_headers(api_key),
        )
    if response.status_code >= 400:
        # The published Custom TTS spec requires GET /voices to be cheap
        # and answer 2xx — a failure here is a real contract violation.
        return False, f"GET /voices failed: {_http_error_detail(response)}"
    body = _safe_json(response)
    voices = body.get("voices") if isinstance(body, dict) else None
    count = len(voices) if isinstance(voices, list) else 0
    return True, f"{count} voices"


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


async def _probe_search(
    entry: ProviderCatalogEntry,
    config: dict[str, Any],
    secret: dict[str, Any],
    *,
    deep: bool,
    transport: httpx.AsyncBaseTransport | None,
) -> list[ProbeReport]:
    # The websearch clients own their httpx stack, so ``transport`` cannot
    # be forwarded here; tests patch ``httpx.AsyncClient`` instead.
    del deep, transport
    runtime = _runtime()
    if entry.id not in runtime._SEARCH_PROVIDERS:
        return [
            _not_probed("search", f"no live search probe for provider {entry.id!r}"),
        ]

    async def check() -> _CheckResult:
        row = ProviderConnection(
            id="live-probe",
            provider=entry.id,
            label=entry.display_name,
            enabled=True,
            capabilities=("search",),
            config=config,
        )
        client = runtime._build_search_client(row, secret)
        response = await client.search(query=_SEARCH_PROBE_QUERY, max_results=1)
        return True, f"{len(response.results)} result(s) for {_SEARCH_PROBE_QUERY!r}"

    return [await _run("search", "searched", check)]


# ---------------------------------------------------------------------------
# image
# ---------------------------------------------------------------------------


async def _probe_image(
    entry: ProviderCatalogEntry,
    config: dict[str, Any],
    secret: dict[str, Any],
    *,
    deep: bool,
    transport: httpx.AsyncBaseTransport | None,
) -> list[ProbeReport]:
    runtime = _runtime()
    api_key = _cfg_str(secret, "api_key")
    if entry.id == "comfyui":
        # Direct-connect ComfyUI: the HTTP endpoint lives under ``server``.
        server = _cfg_str(config, "server").rstrip("/")
        return [
            await _run(
                "image",
                "reachability",
                lambda: _reachable(
                    server, transport, ok_detail=_GATEWAY_REACHABLE_DETAIL,
                ),
            ),
        ]
    defaults = runtime._IMAGE_DEFAULTS.get(entry.id)
    if defaults is None:
        return [
            _not_probed("image", f"no live image probe for provider {entry.id!r}"),
        ]
    default_base, default_model, kind = defaults
    base = (_cfg_str(config, "base_url") or default_base).rstrip("/")
    model = (
        _cfg_str(config, "image_model")
        or _cfg_str(config, "default_model")
        or default_model
    )
    # The probe budget must match what the runtime would actually allow
    # (row timeout_seconds, adapter default 180s) — a slower-but-compliant
    # gateway must not fail the probe while passing at runtime.
    deep_timeout = _cfg_float(config, "timeout_seconds", DEEP_IMAGE_TIMEOUT_SECONDS)
    if deep:
        if kind == "gateway":
            return [
                await _run(
                    "image",
                    "generated_image",
                    lambda: _generate_image(
                        base,
                        api_key,
                        model,
                        # Smallest size the published gateway spec allows —
                        # 512x512 is outside its pinned {1024x1024,
                        # 1024x1536, 1536x1024} set.
                        size="1024x1024",
                        timeout_seconds=deep_timeout,
                        transport=transport,
                    ),
                ),
            ]
        if entry.id == "openai":
            return [
                await _run(
                    "image",
                    "generated_image",
                    lambda: _generate_image(
                        base,
                        api_key,
                        model,
                        size="1024x1024",
                        timeout_seconds=deep_timeout,
                        transport=transport,
                    ),
                ),
            ]
        if entry.id == "openrouter":
            return [
                await _run(
                    "image",
                    "generated_image",
                    lambda: _generate_openrouter_image(
                        base, api_key, model, deep_timeout, transport,
                    ),
                ),
            ]
        # No spec-pinned generation shape for this provider (gemini/xai):
        # run the shallow auth check plus an honest not_probed marker.
        return [
            await _shallow_image_check(entry.id, kind, base, api_key, transport),
            _not_probed(
                "image",
                f"deep image generation probe not supported for {entry.id!r}"
                " — shallow auth check ran instead",
            ),
        ]
    return [await _shallow_image_check(entry.id, kind, base, api_key, transport)]


async def _shallow_image_check(
    provider_id: str,
    kind: str,
    base_url: str,
    api_key: str,
    transport: httpx.AsyncBaseTransport | None,
) -> ProbeReport:
    if kind == "gateway":
        return await _run(
            "image",
            "reachability",
            lambda: _reachable(
                base_url, transport, ok_detail=_GATEWAY_REACHABLE_DETAIL,
            ),
        )
    if provider_id == "google_gemini":
        return await _run(
            "image",
            "listed_models",
            lambda: _gemini_listed_models(base_url, api_key, transport),
        )
    # openai / openrouter / xai all expose a Bearer-authenticated /models.
    return await _run(
        "image",
        "listed_models",
        lambda: _openai_listed_models(base_url, api_key, transport),
    )


async def _gemini_listed_models(
    base_url: str,
    api_key: str,
    transport: httpx.AsyncBaseTransport | None,
) -> _CheckResult:
    if not base_url:
        return False, "base_url is required to list models"
    async with _client(DEFAULT_TIMEOUT_SECONDS, transport) as client:
        response = await client.get(
            f"{base_url}/models",
            headers={"x-goog-api-key": api_key, "Accept": "application/json"},
        )
    if response.status_code >= 400:
        return False, _http_error_detail(response)
    body = _safe_json(response)
    models = body.get("models") if isinstance(body, dict) else None
    count = len(models) if isinstance(models, list) else 0
    return True, f"{count} models"


def _image_headers(api_key: str) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        # Match the documented header shape exactly (spec: img-<hex>) —
        # gateways may key log-correlation on the prefix.
        "X-Request-Id": f"img-{uuid4().hex}",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


async def _generate_image(
    base_url: str,
    api_key: str,
    model: str,
    *,
    size: str,
    timeout_seconds: float = DEEP_IMAGE_TIMEOUT_SECONDS,
    transport: httpx.AsyncBaseTransport | None,
) -> _CheckResult:
    """Deep probe: one real generation via the spec-pinned OpenAI-Images /
    Custom Media Gateway body (docs/CUSTOM_MEDIA_GATEWAY_SPEC.md)."""
    if not base_url or not model:
        return False, "deep image probe requires base_url and image_model"
    payload = {
        "model": model,
        "prompt": _IMAGE_PROBE_PROMPT,
        "size": size,
        "n": 1,
    }
    async with _client(timeout_seconds, transport) as client:
        response = await client.post(
            f"{base_url}/images/generations",
            headers=_image_headers(api_key),
            json=payload,
        )
    return _describe_image_response(response, model)


async def _generate_openrouter_image(
    base_url: str,
    api_key: str,
    model: str,
    timeout_seconds: float,
    transport: httpx.AsyncBaseTransport | None,
) -> _CheckResult:
    """OpenRouter posts ``/images`` (not ``/images/generations``) with a
    minimal {model, prompt, n} body — see OpenRouterImageProvider."""
    if not base_url or not model:
        return False, "deep image probe requires base_url and image_model"
    payload = {"model": model, "prompt": _IMAGE_PROBE_PROMPT, "n": 1}
    async with _client(timeout_seconds, transport) as client:
        response = await client.post(
            f"{base_url}/images",
            headers=_image_headers(api_key),
            json=payload,
        )
    return _describe_image_response(response, model)


def _describe_image_response(response: httpx.Response, model: str) -> _CheckResult:
    if response.status_code >= 400:
        return False, f"model {model!r}: {_http_error_detail(response)}"
    body = _safe_json(response)
    items: Any = None
    if isinstance(body, dict):
        items = body.get("data")
        if not isinstance(items, list):
            items = body.get("artifacts")
    if not isinstance(items, list):
        return False, "image response carried no data array"
    for item in items:
        if not isinstance(item, dict):
            continue
        b64 = item.get("b64_json") or item.get("b64")
        if isinstance(b64, str) and b64:
            try:
                raw = base64.b64decode(b64)
            except Exception:
                return False, "image response carried invalid base64 payload"
            return True, f"generated {len(raw)} bytes (b64_json, model {model!r})"
        url = item.get("url")
        if isinstance(url, str) and url:
            # URL items: presence is the verification — never download.
            return True, (
                f"generated image URL returned (not downloaded, model {model!r})"
            )
    return False, "image response contained no usable image item"


# ---------------------------------------------------------------------------
# video
# ---------------------------------------------------------------------------


async def _probe_video(
    entry: ProviderCatalogEntry,
    config: dict[str, Any],
    secret: dict[str, Any],
    *,
    deep: bool,
    transport: httpx.AsyncBaseTransport | None,
) -> list[ProbeReport]:
    # Never generate — video jobs take minutes even on fast backends, so
    # deep mode intentionally stays a reachability check.
    del deep, secret
    runtime = _runtime()
    defaults = runtime._VIDEO_DEFAULTS.get(entry.id)
    default_base = defaults[0] if defaults else ""
    base = (_cfg_str(config, "base_url") or default_base).rstrip("/")
    return [
        await _run(
            "video",
            "reachability",
            lambda: _reachable(base, transport, ok_detail=_VIDEO_REACHABLE_DETAIL),
        ),
    ]


_CAPABILITY_PROBES: dict[str, Any] = {
    "llm": _probe_llm,
    "embedding": _probe_embedding,
    "tts": _probe_tts,
    "search": _probe_search,
    "image": _probe_image,
    "video": _probe_video,
}

# Action used when a capability handler itself crashes before it could
# build a report — keeps the failure attributable to the right check.
_FALLBACK_ACTIONS: dict[str, str] = {
    "llm": "listed_models",
    "embedding": "embedded",
    "tts": "listed_voices",
    "search": "searched",
    "image": "reachability",
    "video": "reachability",
}
