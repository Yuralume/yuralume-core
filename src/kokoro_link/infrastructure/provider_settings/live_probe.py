"""Live capability probes for BYOK provider connections.

``probe_connection`` upgrades the admin "Test" button from pure local
validation to real network checks. Since the 2026-07-16 unification the
engine is an ORCHESTRATOR, not a second implementation of provider
request shapes: for each capability it synthesizes an in-memory
:class:`ProviderConnection` row from the draft config/secret, builds
the REAL runtime adapter through the same
:mod:`adapter_builders` mapping ``runtime_sync`` uses, and calls the
adapter's optional probe hook (``probe_chat`` / ``probe_embedding`` /
``probe_tts`` / ``probe_image_generation`` — feature-detected via
``getattr``, mirroring the ``validate_reasoning_effort`` precedent).
Adapters own their payloads AND their signal-driven retries, so the
probe automatically inherits every quirk-coping path the runtime learns
— one request shape, owned in exactly one place.

The engine keeps what is not shape ownership: per-capability dispatch,
reachability/shallow checks for adapters without a hook (video, gateway
image auth checks, the Yuralume gateway option listings), timing,
fail-soft semantics, the ``deep`` flag, and secret redaction.

Shared API contract (mirrored by the admin frontend):

* One :class:`ProbeReport` per performed check; ``action`` is pinned to
  the shared enum (``listed_models`` / ``chat_completion`` /
  ``embedded`` / ``listed_voices`` / ``synthesized_speech`` /
  ``searched`` / ``reachability`` / ``generated_image`` /
  ``not_probed`` / ``config_check``; the service layer also emits
  ``config_check`` for local-validation failures before any network is
  touched — the engine reuses it for adapter-construction failures,
  which are config errors by definition).
* Fail-soft: one capability failing never aborts the others, and a
  probe never raises — failures become failed reports.
* A (provider, capability) pair with no probe strategy yields an honest
  ``not_probed`` with ``ok=True`` so the absence of a probe can never
  fail the connection card.
* Every detail string passes :func:`sanitize_error` so probe output can
  never echo an API key.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from kokoro_link.contracts.provider_probe import ProbeCheck
from kokoro_link.contracts.provider_settings import ProviderConnection
from kokoro_link.infrastructure.provider_settings import adapter_builders
from kokoro_link.infrastructure.provider_settings.catalog import ProviderCatalogEntry
from kokoro_link.infrastructure.provider_settings.model_discovery import (
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
# Matches the runtime image adapters' default (adapter_builders: 180s) and
# the published gateway spec — a compliant gateway that generates in
# 121-180s must not fail the probe while passing at runtime.
DEEP_IMAGE_TIMEOUT_SECONDS = 180.0

_SEARCH_PROBE_QUERY = "ping"
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

    ``transport`` is a test seam threaded into the adapters' probe hooks
    and every ``httpx`` client this module builds (the ``search`` branch
    is the one exception — the websearch clients own their HTTP stack,
    so tests patch ``httpx.AsyncClient`` for that branch instead).
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


async def _reports_from_hook(
    capability: str,
    fallback_action: str,
    invoke: Awaitable[list[ProbeCheck]],
) -> list[ProbeReport]:
    """Map an adapter probe hook's checks onto engine ProbeReports.

    The hook owns the request shapes and per-check timing; the engine
    keeps fail-soft (a hook that raises becomes one failed report) and
    the sanitization boundary.
    """
    start = time.perf_counter()
    try:
        checks = await invoke
    except Exception as exc:
        return [
            ProbeReport(
                capability=capability,
                action=fallback_action,
                ok=False,
                detail=sanitize_error(str(exc) or exc.__class__.__name__),
                latency_ms=int((time.perf_counter() - start) * 1000),
            ),
        ]
    return [
        ProbeReport(
            capability=capability,
            action=check.action,
            ok=check.ok,
            detail=sanitize_error(check.detail),
            latency_ms=check.latency_ms,
        )
        for check in checks
    ]


def _config_error(capability: str, exc: Exception) -> list[ProbeReport]:
    """Adapter construction failed — a config error, before any network."""
    return [
        ProbeReport(
            capability=capability,
            action="config_check",
            ok=False,
            detail=sanitize_error(str(exc) or exc.__class__.__name__),
            latency_ms=0,
        ),
    ]


def _not_probed(capability: str, detail: str) -> ProbeReport:
    return ProbeReport(
        capability=capability,
        action="not_probed",
        ok=True,
        detail=sanitize_error(detail),
        latency_ms=0,
    )


def _draft_row(
    entry: ProviderCatalogEntry,
    config: dict[str, Any],
    capability: str,
) -> ProviderConnection:
    """Synthesize an in-memory row so the draft rides the SAME
    row→adapter mapping ``runtime_sync`` uses at boot/save time."""
    return ProviderConnection(
        id="live-probe",
        provider=entry.id,
        label=entry.display_name,
        enabled=True,
        capabilities=(capability,),
        config=config,
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
    if entry.id == "yuralume_cloud":
        api_key = _cfg_str(secret, "api_key")
        base = _cfg_str(config, "base_url").rstrip("/")
        return [
            await _run(
                "llm",
                "listed_models",
                lambda: _gateway_listed_models(base, api_key, "llm", transport),
            ),
        ]
    try:
        model = adapter_builders.build_chat_model(
            _draft_row(entry, config, "llm"), secret,
        )
    except ValueError as exc:
        return _config_error("llm", exc)
    hook = getattr(model, "probe_chat", None)
    if model is None or not callable(hook):
        return [_not_probed("llm", f"no live LLM probe for provider {entry.id!r}")]
    return await _reports_from_hook(
        "llm",
        "chat_completion",
        hook(transport=transport, timeout_seconds=DEFAULT_TIMEOUT_SECONDS),
    )


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
    try:
        embedder = adapter_builders.build_embedder(
            _draft_row(entry, config, "embedding"), secret,
        )
    except ValueError as exc:
        return _config_error("embedding", exc)
    hook = getattr(embedder, "probe_embedding", None)
    if embedder is None or not callable(hook):
        return [
            _not_probed(
                "embedding",
                f"no live embedding probe for provider {entry.id!r}",
            ),
        ]
    return await _reports_from_hook(
        "embedding",
        "embedded",
        hook(transport=transport, timeout_seconds=DEFAULT_TIMEOUT_SECONDS),
    )


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
    if entry.id == "yuralume_cloud":
        return [
            _not_probed(
                "tts",
                "gateway TTS is validated at runtime, not from the admin test",
            ),
        ]
    try:
        built = adapter_builders.build_tts(_draft_row(entry, config, "tts"), secret)
    except ValueError as exc:
        return _config_error("tts", exc)
    hook = getattr(built.port, "probe_tts", None) if built is not None else None
    if built is None or not callable(hook):
        return [_not_probed("tts", f"no live TTS probe for provider {entry.id!r}")]
    return await _reports_from_hook(
        "tts",
        "listed_voices",
        hook(transport=transport, timeout_seconds=DEFAULT_TIMEOUT_SECONDS),
    )


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
    if entry.id not in adapter_builders._SEARCH_PROVIDERS:
        return [
            _not_probed("search", f"no live search probe for provider {entry.id!r}"),
        ]

    async def check() -> _CheckResult:
        # ``search()`` on the runtime client IS the self-test — the
        # builder + client already own the request shape end-to-end.
        client = adapter_builders.build_search_client(
            _draft_row(entry, config, "search"), secret,
        )
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
    defaults = adapter_builders._IMAGE_DEFAULTS.get(entry.id)
    if defaults is None:
        return [
            _not_probed("image", f"no live image probe for provider {entry.id!r}"),
        ]
    default_base, _default_model, kind = defaults
    base = (_cfg_str(config, "base_url") or default_base).rstrip("/")
    if deep:
        # Deep mode: build the REAL runtime image provider from the draft
        # row and delegate to its probe hook — the provider's own payload
        # builder and signal-driven retries do the work.
        try:
            provider = adapter_builders.build_image_provider(
                _draft_row(entry, config, "image"), secret,
            )
        except ValueError as exc:
            return _config_error("image", exc)
        hook = (
            getattr(provider, "probe_image_generation", None)
            if provider is not None
            else None
        )
        if callable(hook):
            # The probe budget must match what the runtime would actually
            # allow (row timeout_seconds, adapter default 180s) — a
            # slower-but-compliant gateway must not fail the probe while
            # passing at runtime.
            deep_timeout = _cfg_float(
                config, "timeout_seconds", DEEP_IMAGE_TIMEOUT_SECONDS,
            )
            return await _reports_from_hook(
                "image",
                "generated_image",
                hook(
                    prompt=_IMAGE_PROBE_PROMPT,
                    transport=transport,
                    timeout_seconds=deep_timeout,
                ),
            )
        # No generation hook for this provider: run the shallow auth
        # check plus an honest not_probed marker.
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
    # deep mode intentionally stays a reachability check owned by the
    # engine (no adapter hook by design).
    del deep, secret
    defaults = adapter_builders._VIDEO_DEFAULTS.get(entry.id)
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
