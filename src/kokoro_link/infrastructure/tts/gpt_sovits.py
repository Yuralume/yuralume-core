"""GPT-SoVITS HTTP adapter.

Targets the upstream ``api_v2.py`` server (the one shipped with the
GPT-SoVITS repo). The endpoint is ``GET /tts`` with query parameters
for inference; the response body is the raw WAV. ``streaming_mode``
is intentionally left off — we want the whole clip in one shot so the
service can hash + cache it on disk before handing back a URL. Stream
mode would be Phase 2 if we ever wire chunked playback into the chat
panel.

Failure modes:

* unreachable backend / connect error → :class:`TTSUnavailable`
  (config issue, treat as "disabled" upstream).
* non-200 / empty body / decode error → :class:`TTSError`
  (backend was there but couldn't synth; retry might help).
"""

from __future__ import annotations

import logging

import httpx

from kokoro_link.contracts.tts import (
    TTSError,
    TTSPort,
    TTSRequest,
    TTSResult,
    TTSUnavailable,
)

_LOGGER = logging.getLogger(__name__)


class GPTSoVITSAdapter(TTSPort):
    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float = 90.0,
    ) -> None:
        if not base_url:
            raise ValueError("GPTSoVITSAdapter base_url must be non-empty")
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        # Cache the last-loaded weights so character-A → character-A
        # repeats don't burn the 5–10 sec model-load each request.
        # Empty string = "we don't know what the server has loaded
        # right now"; first request always triggers a switch in that
        # case. Two-character ping-pong still pays the cost (no way
        # around that without multiple GPT-SoVITS instances).
        self._loaded_gpt: str = ""
        self._loaded_sovits: str = ""

    async def synthesize(self, request: TTSRequest) -> TTSResult:
        await self._maybe_switch_weights(request)
        params = {
            "text": request.text,
            "text_lang": request.text_lang,
            "ref_audio_path": request.ref_audio_path,
            "prompt_text": request.prompt_text,
            "prompt_lang": request.prompt_lang,
            "text_split_method": request.text_split_method,
            "top_k": request.top_k,
            "top_p": request.top_p,
            "temperature": request.temperature,
            "speed_factor": request.speed_factor,
            # ``streaming_mode`` defaults to false on the server, but
            # we set it explicitly so a future server-side flip to
            # streaming-by-default doesn't silently break our buffer.
            "streaming_mode": "false",
            "media_type": "wav",
        }
        url = f"{self._base_url}/tts"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.get(url, params=params)
        except (httpx.ConnectError, httpx.ConnectTimeout):
            raise TTSUnavailable(
                f"GPT-SoVITS server not reachable at {self._base_url}",
            )
        except httpx.HTTPError as exc:
            _LOGGER.exception("GPT-SoVITS request crashed")
            raise TTSError(f"TTS HTTP error: {exc!s}")

        if response.status_code != 200:
            body_preview = response.text[:200] if response.text else ""
            raise TTSError(
                f"GPT-SoVITS returned {response.status_code}: {body_preview}",
            )
        audio = response.content
        if not audio:
            raise TTSError("GPT-SoVITS returned empty body")
        return TTSResult(audio=audio, media_type="audio/wav")

    async def _maybe_switch_weights(self, request: TTSRequest) -> None:
        weights = request.weights
        if weights.gpt_weights_path and weights.gpt_weights_path != self._loaded_gpt:
            await self._call_set_weights(
                "set_gpt_weights", weights.gpt_weights_path,
            )
            self._loaded_gpt = weights.gpt_weights_path
        if (
            weights.sovits_weights_path
            and weights.sovits_weights_path != self._loaded_sovits
        ):
            await self._call_set_weights(
                "set_sovits_weights", weights.sovits_weights_path,
            )
            self._loaded_sovits = weights.sovits_weights_path

    async def _call_set_weights(self, endpoint: str, path: str) -> None:
        url = f"{self._base_url}/{endpoint}"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.get(url, params={"weights_path": path})
        except (httpx.ConnectError, httpx.ConnectTimeout):
            raise TTSUnavailable(
                f"GPT-SoVITS server not reachable at {self._base_url}",
            )
        except httpx.HTTPError as exc:
            raise TTSError(f"weight switch crashed ({endpoint}): {exc!s}")
        if response.status_code != 200:
            body_preview = response.text[:200] if response.text else ""
            raise TTSError(
                f"{endpoint} returned {response.status_code}: {body_preview}",
            )
