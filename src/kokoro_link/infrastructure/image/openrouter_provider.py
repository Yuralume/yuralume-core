"""OpenRouter image-generation adapter.

OpenRouter aggregates image models (FLUX, Seedream, Gemini image, …)
behind a single dedicated endpoint that is *close to* but NOT the
OpenAI Images shape the ``OpenAIImageProvider`` / ``ExternalImageApiProvider``
adapters speak:

  * OpenAI (and the gateway adapter) POST ``{base_url}/images/generations``.
  * OpenRouter POSTs ``{base_url}/images`` (verified 2026-07-05 against
    openrouter.ai/docs — the ``/generations`` suffix 404s there).

The *response* shape, however, matches OpenAI Images: a top-level
``data`` array whose items carry ``b64_json`` (base64 payload, not a
data URL) or ``url``. So we reuse ``native_common`` for prompt
assembly and response decoding, and only the request path/payload is
OpenRouter-specific.

Failure model maps onto :class:`ImageProviderPort`:

  * Network / timeout             → :class:`ImageTimeoutError`
  * Non-2xx HTTP / malformed body → :class:`ImageGenerationError`
  * 2xx with empty data           → :class:`ImageNoOutputError`
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import uuid4

import httpx

from kokoro_link.contracts.image_provider import (
    ImageGenerationError,
    ImageNoOutputError,
    ImageProviderPort,
    ImageTimeoutError,
)
from kokoro_link.infrastructure.image.native_common import (
    build_prompt,
    image_bytes_from_data_array,
    json_or_raise,
)

_MAX_BATCH = 4


class OpenRouterImageProvider(ImageProviderPort):
    """OpenRouter ``POST /api/v1/images`` adapter.

    The request body is intentionally minimal — ``{model, prompt, n}`` —
    because OpenRouter routes to heterogeneous upstream image models
    that each interpret size/aspect differently; sending only the
    fields every upstream accepts keeps the call robust across the
    aggregator's shifting model line-up. Aspect is folded into the
    natural-language prompt rather than a rigid ``size`` enum for the
    same reason.
    """

    provider_id = "openrouter"

    def __init__(
        self,
        *,
        base_url: str = "https://openrouter.ai/api/v1",
        api_key: str,
        model: str,
        timeout_seconds: float = 180.0,
    ) -> None:
        if not api_key.strip():
            raise ValueError("OpenRouter image API api_key is required")
        if not model.strip():
            raise ValueError("OpenRouter image API model is required")
        self._base_url = (base_url or "https://openrouter.ai/api/v1").rstrip("/")
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
            raise ImageGenerationError("OpenRouter image prompt is empty")
        payload = {
            "model": self._model,
            "prompt": prompt,
            "n": max(1, min(int(batch), _MAX_BATCH)),
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(
                    f"{self._base_url}/images",
                    headers=self._headers(),
                    json=payload,
                )
                data = json_or_raise(response, "OpenRouter image")
                return await image_bytes_from_data_array(
                    data,
                    client=client,
                    base_url=self._base_url,
                    label="OpenRouter image API",
                )
        except httpx.TimeoutException as exc:
            raise ImageTimeoutError("OpenRouter image API timed out") from exc
        except ImageGenerationError:
            raise
        except Exception as exc:
            raise ImageGenerationError(str(exc)) from exc

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "X-Request-Id": f"openrouter-img-{uuid4().hex}",
        }
