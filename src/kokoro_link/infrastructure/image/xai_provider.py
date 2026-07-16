"""xAI Grok Imagine native image adapter.

Payload notes (docs.x.ai/developers/model-capabilities/images/generation):

* ``aspect_ratio`` is a grok-imagine-era parameter. Legacy models
  (grok-2-image-*) accept only ``{prompt, n, response_format}`` and the
  endpoint hard-rejects unknown params with HTTP 400
  ``{"code": "400", "error": "Argument not supported: <param>"}``.
  We cope signal-driven (mirroring the chat adapter's
  ``max_tokens``→``max_completion_tokens`` rename): when the server
  names ``aspect_ratio`` as unsupported we retry once without it and
  remember the answer per instance — never a model-name allowlist.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from uuid import uuid4

import httpx

from kokoro_link.contracts.image_provider import (
    ImageGenerationError,
    ImageProviderPort,
    ImageTimeoutError,
)
from kokoro_link.contracts.provider_probe import (
    ProbeCheck,
    probe_http_client,
    run_probe_check,
)
from kokoro_link.infrastructure.image.native_common import (
    ASPECT_TO_RATIO,
    build_prompt,
    describe_image_probe_response,
    image_bytes_from_data_array,
    json_or_raise,
)

_MAX_BATCH = 4

_LOGGER = logging.getLogger(__name__)


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
        # Learned per instance from the server's 400 signal — see module
        # docstring. Starts optimistic (grok-imagine models accept it).
        self._send_aspect_ratio = True

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
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                payload = self._payload(prompt=prompt, aspect=aspect, batch=batch)
                response = await client.post(
                    f"{self._base_url}/images/generations",
                    headers=self._headers(),
                    json=payload,
                )
                if _is_aspect_ratio_unsupported_error(
                    status_code=response.status_code,
                    body=response.text,
                    payload=payload,
                ):
                    # The server named the offending argument — drop it,
                    # remember per instance, retry once.
                    _LOGGER.info(
                        "xAI image model %s rejected aspect_ratio; retrying "
                        "without it",
                        payload.get("model"),
                    )
                    self._send_aspect_ratio = False
                    payload = self._payload(
                        prompt=prompt, aspect=aspect, batch=batch,
                    )
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

    async def probe_image_generation(
        self,
        *,
        prompt: str,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float | None = None,
    ) -> list[ProbeCheck]:
        """Adapter-owned deep image self-test (admin "Test" button).

        One real 1-image generation via THIS adapter's ``_payload`` —
        including the signal-driven ``aspect_ratio`` drop-and-retry and
        its per-instance memo — so the Test button exercises exactly
        what the runtime would send (and learn).
        """

        async def check() -> tuple[bool, str]:
            payload = self._payload(prompt=prompt, aspect="square", batch=1)
            async with probe_http_client(
                timeout_seconds or self._timeout, transport,
            ) as client:
                response = await client.post(
                    f"{self._base_url}/images/generations",
                    headers=self._headers(),
                    json=payload,
                )
                if _is_aspect_ratio_unsupported_error(
                    status_code=response.status_code,
                    body=response.text,
                    payload=payload,
                ):
                    # Same server signal + memo as generate().
                    self._send_aspect_ratio = False
                    payload = self._payload(prompt=prompt, aspect="square", batch=1)
                    response = await client.post(
                        f"{self._base_url}/images/generations",
                        headers=self._headers(),
                        json=payload,
                    )
            return describe_image_probe_response(response, self._model)

        return [await run_probe_check("generated_image", check)]

    def _payload(self, *, prompt: str, aspect: str, batch: int) -> dict:
        payload: dict = {
            "model": self._model,
            "prompt": prompt,
            "n": max(1, min(int(batch), _MAX_BATCH)),
            "response_format": "b64_json",
        }
        if self._send_aspect_ratio:
            payload["aspect_ratio"] = ASPECT_TO_RATIO.get(
                aspect,
                ASPECT_TO_RATIO["portrait"],
            )
        return payload

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "X-Request-Id": f"xai-img-{uuid4().hex}",
        }


def _is_aspect_ratio_unsupported_error(
    *,
    status_code: int,
    body: str,
    payload: dict,
) -> bool:
    """The endpoint rejected ``aspect_ratio`` as an unsupported argument.

    xAI's image endpoint is strict about params the target model does not
    accept: HTTP 400 with ``"Argument not supported: <param>"`` naming the
    offender (open-webui#23611 reproduces the class for ``size``).
    Detection is signal-driven — the server names the parameter — never a
    model-name allowlist, so future models inherit the coping path.

    NOTE: the exact ``aspect_ratio`` error wording is EXTRAPOLATED from
    that documented ``size``-rejection evidence and has not been verified
    against a live xAI key (docs/PROVIDER_COMPAT_AUDIT.md, xAI section —
    confidence "PLAUSIBLE-high"). The match is deliberately loose
    ("aspect_ratio" + "not supported", case-insensitive) so wording
    drift within the same rejection class still triggers; a false
    negative costs only the un-adapted 400 surfacing to the operator.
    """
    if status_code != 400 or "aspect_ratio" not in payload:
        return False
    lowered = body.lower()
    return "aspect_ratio" in lowered and "not supported" in lowered
