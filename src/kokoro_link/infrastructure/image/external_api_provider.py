"""External image capability API adapter.

This adapter speaks Kokoro-Link's normalized image generation shape used by a
media gateway or custom self-host wrapper. It deliberately knows nothing about
ComfyUI workflows, checkpoints, queues, or native provider SDKs.
"""

from __future__ import annotations

import base64
import logging
from collections.abc import Mapping
from urllib.parse import urljoin, urlsplit
from uuid import uuid4

import httpx

from kokoro_link.contracts.image_provider import (
    ImageGenerationError,
    ImageNoOutputError,
    ImageTimeoutError,
)
from kokoro_link.infrastructure.http_error_logging import log_http_error_response
from kokoro_link.infrastructure.prompt.character_identity import (
    render_character_visual_identity_lines,
)
from kokoro_link.infrastructure.prompt.visual_subject import (
    render_character_visual_subject_lines,
)


ASPECT_TO_SIZE: dict[str, str] = {
    "portrait": "1024x1536",
    "landscape": "1536x1024",
    "square": "1024x1024",
}

SUPPORTED_API_PROVIDERS = {"gateway", "custom", "openai_compatible"}

_LOGGER = logging.getLogger(__name__)


class ExternalImageApiProvider:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        provider: str = "gateway",
        timeout_seconds: float = 180.0,
    ) -> None:
        if not base_url.strip():
            raise ValueError("external image API base_url is required")
        if not api_key.strip():
            raise ValueError("external image API api_key is required")
        if not model.strip():
            raise ValueError("external image API model is required")
        provider_name = (provider or "gateway").strip().lower() or "gateway"
        if provider_name not in SUPPORTED_API_PROVIDERS:
            raise ValueError(
                "external image API provider "
                f"{provider_name!r} is not supported by the gateway adapter; "
                "use provider='gateway' for a normalized wrapper or add a "
                "dedicated native adapter"
            )
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._provider = provider_name
        self._timeout = timeout_seconds

    async def generate(
        self,
        *,
        character,
        positive: str,
        aspect: str = "portrait",
        batch: int = 1,
        recent_dialogue: str = "",
        use_runtime_state: bool = True,
        user_attachment_urls=(),
    ) -> list[bytes]:
        prompt = _build_prompt(
            character=character,
            positive=positive,
            recent_dialogue=recent_dialogue,
            use_runtime_state=use_runtime_state,
            user_attachment_urls=user_attachment_urls,
        )
        if not prompt.strip():
            raise ImageGenerationError("image prompt is empty")
        # NOTE: deliberately no ``response_format`` field. The published
        # Custom Media Gateway contract (docs/CUSTOM_MEDIA_GATEWAY_SPEC.md)
        # pins this body to exactly {model, prompt, size, n}, and this one
        # payload serves every kind routed here (gateway / custom /
        # openai_compatible — see runtime_sync). Gateways choose b64_json
        # vs url per item in the response instead.
        payload = {
            "model": self._model,
            "prompt": prompt,
            "size": ASPECT_TO_SIZE.get(aspect, ASPECT_TO_SIZE["portrait"]),
            "n": max(1, min(int(batch), 4)),
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(
                    f"{self._base_url}/images/generations",
                    headers=self._headers(),
                    json=payload,
                )
                data = _json_or_raise(response, "image")
                return await _image_bytes_from_response(
                    data,
                    client=client,
                    base_url=self._base_url,
                )
        except httpx.TimeoutException as exc:
            raise ImageTimeoutError("external image API timed out") from exc
        except ImageGenerationError:
            raise
        except Exception as exc:
            raise ImageGenerationError(str(exc)) from exc

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "X-Request-Id": f"img-{uuid4().hex}",
        }


def _build_prompt(
    *,
    character,
    positive: str,
    recent_dialogue: str,
    use_runtime_state: bool,
    user_attachment_urls,
) -> str:
    parts = [
        f"Character: {character.name}",
        f"Appearance: {getattr(character, 'appearance', '')}",
        *render_character_visual_identity_lines(character),
        *render_character_visual_subject_lines(character),
    ]
    if use_runtime_state:
        state = getattr(character, "state", None)
        if state is not None:
            parts.append(f"Current emotion: {getattr(state, 'emotion', '')}")
            intent = getattr(state, "current_intent", None)
            if intent:
                parts.append(f"Current intent: {intent}")
    scene = (positive or "").strip()
    if scene:
        parts.append(f"Scene: {scene}")
    if recent_dialogue.strip():
        parts.append(f"Recent dialogue context: {recent_dialogue.strip()}")
    if user_attachment_urls:
        parts.append(
            "User visual references: "
            + ", ".join(str(url) for url in user_attachment_urls),
        )
    return "\n".join(part for part in parts if part.strip())


async def _image_bytes_from_response(
    data: Mapping,
    *,
    client: httpx.AsyncClient,
    base_url: str,
) -> list[bytes]:
    out: list[bytes] = []
    items = data.get("data")
    if not isinstance(items, list):
        raise ImageNoOutputError("external image API returned no data array")
    for item in items:
        if not isinstance(item, Mapping):
            continue
        b64 = item.get("b64_json")
        if isinstance(b64, str) and b64:
            out.append(base64.b64decode(b64))
            continue
        url = item.get("url")
        if isinstance(url, str) and url:
            out.append(await _download_bytes(client=client, url=url, base_url=base_url))
    if not out:
        raise ImageNoOutputError("external image API produced no images")
    return out


async def _download_bytes(
    *,
    client: httpx.AsyncClient,
    url: str,
    base_url: str,
) -> bytes:
    resolved = url if url.startswith(("http://", "https://")) else urljoin(
        f"{base_url}/",
        url.lstrip("/"),
    )
    # Name only scheme://host in errors — artifact URLs may carry a
    # capability token in the path/query, and the message surfaces in
    # user-facing error details.
    host = _host_of(resolved)
    try:
        response = await client.get(resolved)
    except httpx.TimeoutException:
        # Let the caller's timeout mapping (ImageTimeoutError) handle it.
        raise
    except httpx.HTTPError as exc:
        raise ImageGenerationError(
            f"image artifact download from {host} failed: {exc}",
        ) from exc
    if response.status_code >= 400:
        log_http_error_response(
            _LOGGER,
            response,
            operation="image artifact download",
        )
        raise ImageGenerationError(
            "image artifact download from "
            f"{host} failed: HTTP {response.status_code}",
        )
    return response.content


def _host_of(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme and parts.netloc:
        return f"{parts.scheme}://{parts.netloc}"
    return url


def _json_or_raise(response: httpx.Response, label: str) -> Mapping:
    if response.status_code >= 400:
        log_http_error_response(
            _LOGGER,
            response,
            operation=f"{label} API",
        )
        raise ImageGenerationError(
            f"{label} API error {response.status_code}: {response.text}",
        )
    payload = response.json()
    if not isinstance(payload, Mapping):
        raise ImageGenerationError(f"{label} API returned non-object JSON")
    return payload
