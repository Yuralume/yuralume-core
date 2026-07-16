"""xAI Grok Imagine native image adapter."""

from __future__ import annotations

from collections.abc import Sequence
from uuid import uuid4

import httpx

from kokoro_link.contracts.image_provider import (
    ImageGenerationError,
    ImageProviderPort,
    ImageTimeoutError,
)
from kokoro_link.infrastructure.image.native_common import (
    ASPECT_TO_RATIO,
    build_prompt,
    image_bytes_from_data_array,
    json_or_raise,
)

_MAX_BATCH = 4


class XAIImageProvider(ImageProviderPort):
    def __init__(
        self,
        *,
        base_url: str = "https://api.x.ai/v1",
        api_key: str,
        model: str = "grok-imagine-image-quality",
        timeout_seconds: float = 180.0,
    ) -> None:
        if not api_key.strip():
            raise ValueError("xAI image API api_key is required")
        if not model.strip():
            raise ValueError("xAI image API model is required")
        self._base_url = (base_url or "https://api.x.ai/v1").rstrip("/")
        self._api_key = api_key
        self._model = model
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
        user_attachment_urls: Sequence[str] = (),
    ) -> list[bytes]:
        prompt = build_prompt(
            character=character,
            positive=positive,
            recent_dialogue=recent_dialogue,
            use_runtime_state=use_runtime_state,
            user_attachment_urls=user_attachment_urls,
        )
        if not prompt.strip():
            raise ImageGenerationError("xAI image prompt is empty")
        payload = {
            "model": self._model,
            "prompt": prompt,
            "aspect_ratio": ASPECT_TO_RATIO.get(
                aspect,
                ASPECT_TO_RATIO["portrait"],
            ),
            "n": max(1, min(int(batch), _MAX_BATCH)),
            "response_format": "b64_json",
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(
                    f"{self._base_url}/images/generations",
                    headers=self._headers(),
                    json=payload,
                )
                data = json_or_raise(response, "xAI image")
                return await image_bytes_from_data_array(
                    data,
                    client=client,
                    base_url=self._base_url,
                    label="xAI image API",
                )
        except httpx.TimeoutException as exc:
            raise ImageTimeoutError("xAI image API timed out") from exc
        except ImageGenerationError:
            raise
        except Exception as exc:
            raise ImageGenerationError(str(exc)) from exc

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "X-Request-Id": f"xai-img-{uuid4().hex}",
        }
