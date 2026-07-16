"""Probe a provider's "list available models" endpoint.

The BYOK admin UI uses this to populate a model picker, so the user
isn't typing model IDs blind. Each provider exposes the catalogue at a
slightly different path, but the response is always normalised to a
flat ``list[str]`` of model IDs (label / metadata stripped). Providers
without a discovery endpoint return an empty list — the UI falls back
to a plain text input.

Discovery never raises; errors are turned into ``ModelDiscoveryResult``
so the route can decide how loudly to surface them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx


_CAPABILITY_TO_GATEWAY_PATH: dict[str, str] = {
    "llm": "llm-options",
    "image": "image-options",
    "video": "video-options",
}

_OPENAI_COMPATIBLE_ADAPTERS = {"openai", "openai_compatible"}


@dataclass(frozen=True, slots=True)
class ModelDiscoveryResult:
    models: list[str] = field(default_factory=list)
    error: str | None = None


async def discover_models(
    *,
    provider_id: str,
    adapter_kind: str,
    capability: str,
    base_url: str,
    api_key: str,
    timeout_seconds: float = 15.0,
) -> ModelDiscoveryResult:
    """Return the available model IDs for one draft connection.

    ``provider_id`` is the catalog entry id (``yuralume_cloud`` etc.) and
    ``adapter_kind`` is the catalog ``adapter_kind`` ('openai',
    'openai_compatible', 'yuralume_cloud', …). They jointly decide the
    discovery strategy: yuralume_cloud has per-capability ``/v1/{cap}-
    options``, OpenAI-compatible adapters speak ``/v1/models``, the rest
    have no discovery endpoint.
    """
    cleaned_base = (base_url or "").strip().rstrip("/")
    if not cleaned_base:
        return ModelDiscoveryResult(error="base_url is required to list models")

    try:
        if provider_id == "yuralume_cloud":
            return await _yuralume_cloud(
                base_url=cleaned_base,
                api_key=api_key,
                capability=capability,
                timeout_seconds=timeout_seconds,
            )
        if adapter_kind in _OPENAI_COMPATIBLE_ADAPTERS:
            return await _openai_compatible(
                base_url=cleaned_base,
                api_key=api_key,
                timeout_seconds=timeout_seconds,
            )
    except httpx.HTTPError as exc:
        return ModelDiscoveryResult(error=f"http error: {exc}")
    except Exception as exc:  # pragma: no cover — defensive net
        return ModelDiscoveryResult(error=f"discovery failed: {exc}")
    return ModelDiscoveryResult(
        error=f"model discovery not supported for provider {provider_id!r}",
    )


# ---------------------------------------------------------------------------
# Per-provider strategies
# ---------------------------------------------------------------------------


def gateway_options_url(base_url: str, capability: str) -> str | None:
    """URL of the Yuralume gateway ``/v1/{capability}-options`` endpoint.

    The user usually pastes the gateway base with a trailing /v1 already,
    because that's also what the image/llm clients expect. Tolerate both
    forms so the UI never has to coach them. Returns ``None`` for
    capabilities the gateway exposes no discovery endpoint for. Shared
    with the live-probe engine so both features agree on the URL rule.
    """
    path = _CAPABILITY_TO_GATEWAY_PATH.get(capability)
    if path is None:
        return None
    cleaned_base = (base_url or "").strip().rstrip("/")
    return (
        f"{cleaned_base}/{path}"
        if cleaned_base.endswith("/v1")
        else f"{cleaned_base}/v1/{path}"
    )


async def _yuralume_cloud(
    *,
    base_url: str,
    api_key: str,
    capability: str,
    timeout_seconds: float,
) -> ModelDiscoveryResult:
    url = gateway_options_url(base_url, capability)
    if url is None:
        return ModelDiscoveryResult(
            error=f"yuralume_cloud does not expose discovery for capability "
            f"{capability!r}",
        )
    headers = _bearer_headers(api_key)
    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        response = await client.get(url, headers=headers)
    return _parse_gateway_options(response)


async def _openai_compatible(
    *,
    base_url: str,
    api_key: str,
    timeout_seconds: float,
) -> ModelDiscoveryResult:
    url = f"{base_url}/models"
    headers = _bearer_headers(api_key)
    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        response = await client.get(url, headers=headers)
    if response.status_code >= 400:
        return ModelDiscoveryResult(
            error=f"{response.status_code} {response.reason_phrase}",
        )
    return ModelDiscoveryResult(models=_extract_openai_model_ids(response.json()))


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


def _parse_gateway_options(response: httpx.Response) -> ModelDiscoveryResult:
    if response.status_code >= 400:
        return ModelDiscoveryResult(
            error=f"{response.status_code} {response.reason_phrase}",
        )
    try:
        payload = response.json()
    except ValueError:
        return ModelDiscoveryResult(error="gateway returned non-JSON response")
    return ModelDiscoveryResult(models=_extract_gateway_model_ids(payload))


def _extract_gateway_model_ids(payload: Any) -> list[str]:
    """Pull model IDs out of the Yuralume gateway ``*-options`` payload.

    The gateway shape is in flux — earlier builds returned a bare list of
    strings, current builds wrap them as ``{"options": [{"id": ..., ...}]}``.
    Handle both so admin UI keeps working across gateway versions.
    """
    if isinstance(payload, list):
        return [str(item) for item in payload if isinstance(item, str) and item.strip()]
    if isinstance(payload, dict):
        for key in ("options", "models", "data"):
            entries = payload.get(key)
            if isinstance(entries, list):
                return _flatten_ids(entries)
    return []


def _extract_openai_model_ids(payload: Any) -> list[str]:
    if isinstance(payload, dict):
        entries = payload.get("data")
        if isinstance(entries, list):
            return _flatten_ids(entries)
    if isinstance(payload, list):
        return _flatten_ids(payload)
    return []


def _flatten_ids(entries: list[Any]) -> list[str]:
    out: list[str] = []
    for entry in entries:
        if isinstance(entry, str):
            cleaned = entry.strip()
            if cleaned:
                out.append(cleaned)
        elif isinstance(entry, dict):
            value = entry.get("id") or entry.get("model") or entry.get("name")
            if isinstance(value, str) and value.strip():
                out.append(value.strip())
    return out


def _bearer_headers(api_key: str) -> dict[str, str]:
    headers = {"Accept": "application/json"}
    if api_key and api_key.strip():
        headers["Authorization"] = f"Bearer {api_key.strip()}"
    return headers
