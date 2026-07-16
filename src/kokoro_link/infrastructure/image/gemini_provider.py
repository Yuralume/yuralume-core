"""Google Gemini native image adapter for Nano Banana models."""

from __future__ import annotations

import base64
from collections.abc import Mapping, Sequence
from uuid import uuid4

import httpx

from kokoro_link.contracts.image_provider import (
    ImageGenerationError,
    ImageNoOutputError,
    ImageProviderPort,
    ImageTimeoutError,
)
from kokoro_link.contracts.provider_probe import (
    ProbeCheck,
    probe_http_client,
    probe_http_error_detail,
    run_probe_check,
)
from kokoro_link.infrastructure.image.native_common import (
    ASPECT_TO_RATIO,
    build_prompt,
    json_or_raise,
)

_MAX_BATCH = 4


class GeminiImageProvider(ImageProviderPort):
    def __init__(
        self,
        *,
        base_url: str = "https://generativelanguage.googleapis.com/v1beta",
        api_key: str,
        # gemini-2.5-flash-image shuts down 2026-10-02; Google's announced
        # replacement (ai.google.dev/gemini-api/docs/deprecations) — keep
        # aligned with catalog default_models + runtime_sync._IMAGE_DEFAULTS.
        model: str = "gemini-3.1-flash-image-preview",
        timeout_seconds: float = 180.0,
    ) -> None:
        if not api_key.strip():
            raise ValueError("Gemini image API api_key is required")
        if not model.strip():
            raise ValueError("Gemini image API model is required")
        self._base_url = (
            base_url or "https://generativelanguage.googleapis.com/v1beta"
        ).rstrip("/")
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
            raise ImageGenerationError("Gemini image prompt is empty")
        count = max(1, min(int(batch), _MAX_BATCH))
        images: list[bytes] = []
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                for _ in range(count):
                    response = await client.post(
                        f"{self._base_url}/models/{self._model}:generateContent",
                        headers=self._headers(),
                        json=self._payload(prompt=prompt, aspect=aspect),
                    )
                    data = json_or_raise(response, "Gemini image")
                    images.extend(_images_from_generate_content(data))
        except httpx.TimeoutException as exc:
            raise ImageTimeoutError("Gemini image API timed out") from exc
        except ImageGenerationError:
            raise
        except Exception as exc:
            raise ImageGenerationError(str(exc)) from exc
        if not images:
            raise ImageNoOutputError("Gemini image API produced no images")
        return images

    async def probe_image_generation(
        self,
        *,
        prompt: str,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float | None = None,
    ) -> list[ProbeCheck]:
        """Adapter-owned deep image self-test (admin "Test" button).

        One real ``generateContent`` call via THIS adapter's ``_payload``
        (``generationConfig.imageConfig.aspectRatio``) and response
        parsing, so the Test button exercises exactly the runtime shape.
        """

        async def check() -> tuple[bool, str]:
            async with probe_http_client(
                timeout_seconds or self._timeout, transport,
            ) as client:
                response = await client.post(
                    f"{self._base_url}/models/{self._model}:generateContent",
                    headers=self._headers(),
                    json=self._payload(prompt=prompt, aspect="square"),
                )
            if response.status_code >= 400:
                return False, (
                    f"model {self._model!r}: {probe_http_error_detail(response)}"
                )
            try:
                body = response.json()
            except ValueError:
                return False, "image response was not JSON"
            if not isinstance(body, Mapping):
                return False, "image response was not a JSON object"
            try:
                images = _images_from_generate_content(body)
            except ImageNoOutputError:
                return False, "image response carried no inline image data"
            return True, (
                f"generated {len(images[0])} bytes "
                f"(inlineData, model {self._model!r})"
            )

        return [await run_probe_check("generated_image", check)]

    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "x-goog-api-key": self._api_key,
            "X-Request-Id": f"gemini-img-{uuid4().hex}",
        }

    @staticmethod
    def _payload(*, prompt: str, aspect: str) -> dict:
        # Documented GenerateContentRequest shape for the image models:
        # aspect ratio lives at generationConfig.imageConfig.aspectRatio
        # (https://ai.google.dev/gemini-api/docs/image-generation). The
        # native generativelanguage API hard-rejects unknown JSON fields
        # ("Invalid JSON payload received. Unknown name ..."), so no
        # extra/legacy keys may ride along.
        return {
            "contents": [{
                "parts": [{"text": prompt}],
            }],
            "generationConfig": {
                "imageConfig": {
                    "aspectRatio": ASPECT_TO_RATIO.get(
                        aspect,
                        ASPECT_TO_RATIO["portrait"],
                    ),
                },
            },
        }


def _images_from_generate_content(data: Mapping) -> list[bytes]:
    out: list[bytes] = []
    for part in _iter_parts(data):
        inline = part.get("inlineData") or part.get("inline_data")
        if not isinstance(inline, Mapping):
            continue
        raw = inline.get("data")
        if isinstance(raw, str) and raw:
            out.append(base64.b64decode(raw))
    if not out:
        raise ImageNoOutputError("Gemini image API returned no inline image data")
    return out


def _iter_parts(data: Mapping):
    candidates = data.get("candidates")
    if isinstance(candidates, list):
        for candidate in candidates:
            if not isinstance(candidate, Mapping):
                continue
            content = candidate.get("content")
            if not isinstance(content, Mapping):
                continue
            parts = content.get("parts")
            if isinstance(parts, list):
                for part in parts:
                    if isinstance(part, Mapping):
                        yield part
    parts = data.get("parts")
    if isinstance(parts, list):
        for part in parts:
            if isinstance(part, Mapping):
                yield part
