"""Shared helpers for hosted image provider adapters."""

from __future__ import annotations

import base64
import logging
from collections.abc import Mapping, Sequence
from urllib.parse import urljoin

import httpx

from kokoro_link.contracts.image_provider import (
    ImageGenerationError,
    ImageNoOutputError,
)
from kokoro_link.infrastructure.http_error_logging import log_http_error_response
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

_LOGGER = logging.getLogger(__name__)


def build_prompt(
    *,
    character,
    positive: str,
    recent_dialogue: str,
    use_runtime_state: bool,
    user_attachment_urls: Sequence[str] = (),
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
            emotion = getattr(state, "emotion", "")
            if emotion:
                parts.append(f"Current emotion: {emotion}")
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


async def image_bytes_from_data_array(
    data: Mapping,
    *,
    client: httpx.AsyncClient,
    base_url: str,
    label: str,
) -> list[bytes]:
    out: list[bytes] = []
    items = data.get("data")
    if not isinstance(items, list):
        raise ImageNoOutputError(f"{label} returned no data array")
    for item in items:
        if not isinstance(item, Mapping):
            continue
        b64 = item.get("b64_json") or item.get("b64")
        if isinstance(b64, str) and b64:
            out.append(base64.b64decode(b64))
            continue
        url = item.get("url")
        if isinstance(url, str) and url:
            out.append(
                await download_bytes(client=client, url=url, base_url=base_url),
            )
    if not out:
        raise ImageNoOutputError(f"{label} produced no images")
    return out


async def download_bytes(
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
        log_http_error_response(
            _LOGGER,
            response,
            operation="image artifact download",
        )
        raise ImageGenerationError(
            f"image artifact download failed: {response.status_code}",
        )
    return response.content


def json_or_raise(response: httpx.Response, label: str) -> Mapping:
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
