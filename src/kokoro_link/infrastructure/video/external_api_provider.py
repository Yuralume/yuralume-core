"""External video capability API adapter.

This adapter speaks Kokoro-Link's normalized video generation shape used by a
media gateway or custom self-host wrapper. Native provider APIs need dedicated
adapters or an external wrapper that normalizes their request/response shape.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping
from urllib.parse import urljoin
from uuid import uuid4

import httpx

from kokoro_link.contracts.video_provider import (
    VideoGenerationError,
    VideoNoOutputError,
    VideoTimeoutError,
)
from kokoro_link.infrastructure.prompt.character_identity import (
    render_character_visual_identity_lines,
)
from kokoro_link.infrastructure.prompt.visual_subject import (
    render_character_visual_subject_lines,
)


ASPECT_TO_RATIO: dict[str, str] = {
    "portrait": "9:16",
    "landscape": "16:9",
    "square": "1:1",
}

SUPPORTED_API_PROVIDERS = {"gateway", "custom", "openai_compatible"}


class ExternalVideoApiProvider:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        provider: str = "gateway",
        timeout_seconds: float = 1800.0,
    ) -> None:
        if not base_url.strip():
            raise ValueError("external video API base_url is required")
        if not api_key.strip():
            raise ValueError("external video API api_key is required")
        if not model.strip():
            raise ValueError("external video API model is required")
        provider_name = (provider or "gateway").strip().lower() or "gateway"
        if provider_name not in SUPPORTED_API_PROVIDERS:
            raise ValueError(
                "external video API provider "
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
        length_frames: int = 81,
        recent_dialogue: str = "",
        use_runtime_state: bool = True,
    ) -> bytes:
        prompt = _build_prompt(
            character=character,
            positive=positive,
            recent_dialogue=recent_dialogue,
            use_runtime_state=use_runtime_state,
        )
        if not prompt.strip():
            raise VideoGenerationError("video prompt is empty")
        duration = max(1, round(max(1, int(length_frames)) / 16))
        payload = {
            "model": self._model,
            "prompt": prompt,
            "aspect_ratio": ASPECT_TO_RATIO.get(
                aspect,
                ASPECT_TO_RATIO["portrait"],
            ),
            "duration_seconds": duration,
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(
                    f"{self._base_url}/videos/generations",
                    headers=self._headers(),
                    json=payload,
                )
                data = _json_or_raise(response)
                return await _video_bytes_from_response(
                    data,
                    client=client,
                    base_url=self._base_url,
                )
        except httpx.TimeoutException as exc:
            raise VideoTimeoutError("external video API timed out") from exc
        except VideoGenerationError:
            raise
        except Exception as exc:
            raise VideoGenerationError(str(exc)) from exc

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "X-Request-Id": f"vid-{uuid4().hex}",
        }


def _build_prompt(
    *,
    character,
    positive: str,
    recent_dialogue: str,
    use_runtime_state: bool,
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
    if positive.strip():
        parts.append(f"Scene: {positive.strip()}")
    if recent_dialogue.strip():
        parts.append(f"Recent dialogue context: {recent_dialogue.strip()}")
    return "\n".join(part for part in parts if part.strip())


async def _video_bytes_from_response(
    data: Mapping,
    *,
    client: httpx.AsyncClient,
    base_url: str,
) -> bytes:
    items = data.get("data") or data.get("artifacts")
    if not isinstance(items, list):
        raise VideoNoOutputError("external video API returned no data array")
    for item in items:
        if not isinstance(item, Mapping):
            continue
        b64 = item.get("b64_json") or item.get("b64")
        if isinstance(b64, str) and b64:
            return base64.b64decode(b64)
        url = item.get("url")
        if isinstance(url, str) and url:
            return await _download_bytes(client=client, url=url, base_url=base_url)
    raise VideoNoOutputError("external video API produced no video")


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
    response = await client.get(resolved)
    if response.status_code >= 400:
        raise VideoGenerationError(
            f"video artifact download failed: {response.status_code}",
        )
    return response.content


def _json_or_raise(response: httpx.Response) -> Mapping:
    if response.status_code >= 400:
        raise VideoGenerationError(
            f"video API error {response.status_code}: {response.text}",
        )
    payload = response.json()
    if not isinstance(payload, Mapping):
        raise VideoGenerationError("video API returned non-object JSON")
    return payload
