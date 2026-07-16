"""External TTS capability API adapters."""

from __future__ import annotations

from collections.abc import Mapping
from uuid import uuid4

import httpx

from kokoro_link.contracts.tts import (
    TTSError,
    TTSRequest,
    TTSResult,
    TTSUnavailable,
)
from kokoro_link.contracts.tts_catalog import TTSVoice


class ExternalTTSAdapter:
    """Adapter for ``GET /v1/voices`` + ``POST /v1/tts/synthesize``."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str = "",
        default_voice_id: str = "",
        timeout_seconds: float = 90.0,
    ) -> None:
        if not base_url.strip():
            raise ValueError("external TTS API base_url is required")
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._default_voice_id = default_voice_id
        self._timeout = timeout_seconds

    async def list_voices(self) -> list[TTSVoice]:
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.get(
                    f"{self._base_url}/voices",
                    headers=self._headers(),
                )
            if response.status_code >= 400:
                raise TTSUnavailable(
                    f"TTS voice catalog unavailable: {response.status_code}",
                )
            data = response.json()
        except httpx.HTTPError as exc:
            raise TTSUnavailable("TTS voice catalog unreachable") from exc
        voices = data.get("voices") if isinstance(data, Mapping) else None
        if not isinstance(voices, list):
            return []
        out: list[TTSVoice] = []
        for item in voices:
            if not isinstance(item, Mapping):
                continue
            voice_id = str(item.get("id") or "").strip()
            if not voice_id:
                continue
            out.append(
                TTSVoice(
                    id=voice_id,
                    label=str(item.get("label") or voice_id),
                    prompt_lang=str(item.get("prompt_lang") or ""),
                    is_complete=bool(item.get("is_complete", True)),
                ),
            )
        return out

    async def synthesize(self, request: TTSRequest) -> TTSResult:
        voice_id = (request.voice_id or self._default_voice_id).strip()
        if not voice_id:
            raise TTSUnavailable("TTS voice_id is not configured")
        payload = {
            "text": request.text,
            "voice_id": voice_id,
            "feature_key": "chat",
            "options": {
                "text_lang": request.text_lang,
                "prompt_lang": request.prompt_lang,
                "speed_factor": request.speed_factor,
            },
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(
                    f"{self._base_url}/tts/synthesize",
                    headers=self._headers(),
                    json=payload,
                )
        except httpx.TimeoutException as exc:
            raise TTSError("TTS API timed out") from exc
        except httpx.HTTPError as exc:
            raise TTSUnavailable("TTS API unreachable") from exc
        if response.status_code == 404:
            raise TTSUnavailable("TTS voice not found")
        if response.status_code >= 400:
            raise TTSError(
                f"TTS API error {response.status_code}: {response.text}",
            )
        return TTSResult(
            audio=response.content,
            media_type=response.headers.get("content-type") or "audio/wav",
        )

    def _headers(self) -> dict[str, str]:
        headers = {"X-Request-Id": f"tts-{uuid4().hex}"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers


class OpenAITTSAdapter(ExternalTTSAdapter):
    """Direct OpenAI speech endpoint adapter.

    This is useful when deployments want BYOK without running a gateway. The
    voice catalog is static because OpenAI exposes product voices rather than a
    per-deployment voice service.
    """

    _VOICES = (
        "alloy",
        "ash",
        "ballad",
        "coral",
        "echo",
        "fable",
        "nova",
        "onyx",
        "sage",
        "shimmer",
        "verse",
        "marin",
        "cedar",
    )

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "gpt-4o-mini-tts",
        default_voice_id: str = "marin",
        response_format: str = "wav",
        base_url: str = "https://api.openai.com/v1",
        timeout_seconds: float = 90.0,
    ) -> None:
        if not api_key.strip():
            raise ValueError("OpenAI TTS api_key is required")
        super().__init__(
            base_url=base_url,
            api_key=api_key,
            default_voice_id=default_voice_id,
            timeout_seconds=timeout_seconds,
        )
        self._model = model or "gpt-4o-mini-tts"
        self._response_format = response_format or "wav"

    async def list_voices(self) -> list[TTSVoice]:
        return [
            TTSVoice(id=voice, label=voice, prompt_lang="", is_complete=True)
            for voice in self._VOICES
        ]

    async def synthesize(self, request: TTSRequest) -> TTSResult:
        voice_id = (request.voice_id or self._default_voice_id).strip()
        if not voice_id:
            raise TTSUnavailable("OpenAI TTS voice is not configured")
        payload = {
            "model": self._model,
            "voice": voice_id,
            "input": request.text,
            "response_format": self._response_format,
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(
                    f"{self._base_url}/audio/speech",
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                        "X-Request-Id": f"tts-{uuid4().hex}",
                    },
                    json=payload,
                )
        except httpx.TimeoutException as exc:
            raise TTSError("OpenAI TTS timed out") from exc
        except httpx.HTTPError as exc:
            raise TTSUnavailable("OpenAI TTS unreachable") from exc
        if response.status_code >= 400:
            raise TTSError(
                f"OpenAI TTS error {response.status_code}: {response.text}",
            )
        return TTSResult(
            audio=response.content,
            media_type=response.headers.get("content-type") or _media_type(
                self._response_format,
            ),
        )


def _media_type(fmt: str) -> str:
    value = fmt.lower()
    if value == "mp3":
        return "audio/mpeg"
    if value == "ogg":
        return "audio/ogg"
    if value == "pcm":
        return "audio/pcm"
    return "audio/wav"
