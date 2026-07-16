"""External TTS capability API adapters."""

from __future__ import annotations

from collections.abc import Mapping
from uuid import uuid4

import httpx

from kokoro_link.contracts.provider_probe import (
    PROBE_TTS_TEXT,
    ProbeCheck,
    probe_http_client,
    probe_http_error_detail,
    run_probe_check,
)
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
        return _parse_voice_catalog(data)

    async def probe_tts(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = 15.0,
    ) -> list[ProbeCheck]:
        """Adapter-owned live self-test for the admin "Test" button.

        The published Custom TTS spec requires ``GET /voices`` to be
        cheap and answer 2xx, so listing voices is the probe — no billed
        synthesis. Uses THIS adapter's URL/headers/response parsing.
        """

        async def check() -> tuple[bool, str]:
            async with probe_http_client(timeout_seconds, transport) as client:
                response = await client.get(
                    f"{self._base_url}/voices",
                    headers=self._headers(),
                )
            if response.status_code >= 400:
                # A failure here is a real contract violation.
                return False, (
                    f"GET /voices failed: {probe_http_error_detail(response)}"
                )
            try:
                data = response.json()
            except ValueError:
                return False, "voices endpoint returned non-JSON response"
            return True, f"{len(_parse_voice_catalog(data))} voices"

        return [await run_probe_check("listed_voices", check)]

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


def _parse_voice_catalog(data: object) -> list[TTSVoice]:
    """Parse a ``GET /voices`` payload — shared by runtime and probe."""
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

    def _speech_payload(self, *, text: str, voice_id: str) -> dict[str, str]:
        """OpenAI speech-protocol body — shared by runtime and probe."""
        return {
            "model": self._model,
            "voice": voice_id,
            "input": text,
            "response_format": self._response_format,
        }

    def _speech_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "X-Request-Id": f"tts-{uuid4().hex}",
        }

    async def synthesize(self, request: TTSRequest) -> TTSResult:
        voice_id = (request.voice_id or self._default_voice_id).strip()
        if not voice_id:
            raise TTSUnavailable("OpenAI TTS voice is not configured")
        payload = self._speech_payload(text=request.text, voice_id=voice_id)
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(
                    f"{self._base_url}/audio/speech",
                    headers=self._speech_headers(),
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

    async def probe_tts(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = 15.0,
    ) -> list[ProbeCheck]:
        """OpenAI speech-protocol self-test: synthesize one short word.

        Overrides the custom-TTS voices check because this family's
        voice catalog is static/remote-model-scoped — a real synthesis
        with THIS adapter's exact model/voice/response_format payload is
        the meaningful save-time signal (and the OpenRouter subclass
        inherits it with its own defaults).
        """

        async def check() -> tuple[bool, str]:
            voice_id = self._default_voice_id.strip()
            payload = self._speech_payload(text=PROBE_TTS_TEXT, voice_id=voice_id)
            async with probe_http_client(timeout_seconds, transport) as client:
                response = await client.post(
                    f"{self._base_url}/audio/speech",
                    headers=self._speech_headers(),
                    json=payload,
                )
            if response.status_code >= 400:
                return False, probe_http_error_detail(response)
            return True, (
                f"{len(response.content)} bytes of audio (voice {voice_id!r})"
            )

        return [await run_probe_check("synthesized_speech", check)]


class OpenRouterTTSAdapter(OpenAITTSAdapter):
    """OpenRouter ``/audio/speech`` adapter (OpenAI speech protocol).

    Differences from direct OpenAI (openrouter.ai/docs/guides/overview/
    multimodal/tts, verified 2026-07-16):

    * ``response_format`` accepts only ``mp3`` | ``pcm`` (schema-validated
      before auth; ``wav`` is rejected with a ZodError) → default mp3.
    * Voices are provider-namespaced per model — the static OpenAI voice
      list is wrong for every non-OpenAI TTS model. The authoritative
      catalog is ``GET /models?output_modalities=speech`` whose entries
      carry a ``supported_voices`` array; we surface the configured
      model's voices.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "x-ai/grok-voice-tts-1.0",
        default_voice_id: str = "eve",
        response_format: str = "mp3",
        base_url: str = "https://openrouter.ai/api/v1",
        timeout_seconds: float = 90.0,
    ) -> None:
        super().__init__(
            api_key=api_key,
            model=model or "x-ai/grok-voice-tts-1.0",
            default_voice_id=default_voice_id or "eve",
            response_format=response_format or "mp3",
            base_url=base_url or "https://openrouter.ai/api/v1",
            timeout_seconds=timeout_seconds,
        )

    async def list_voices(self) -> list[TTSVoice]:
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.get(
                    f"{self._base_url}/models",
                    params={"output_modalities": "speech"},
                    headers=self._headers(),
                )
        except httpx.HTTPError as exc:
            raise TTSUnavailable(
                "OpenRouter TTS model catalog unreachable",
            ) from exc
        if response.status_code >= 400:
            raise TTSUnavailable(
                f"OpenRouter TTS model catalog unavailable: "
                f"{response.status_code}",
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise TTSUnavailable(
                "OpenRouter TTS model catalog returned malformed JSON",
            ) from exc
        models = body.get("data") if isinstance(body, Mapping) else None
        if not isinstance(models, list):
            return []
        for entry in models:
            if not isinstance(entry, Mapping):
                continue
            if str(entry.get("id") or "") != self._model:
                continue
            voices = entry.get("supported_voices")
            if not isinstance(voices, list):
                return []
            return [
                TTSVoice(
                    id=str(voice),
                    label=str(voice),
                    prompt_lang="",
                    is_complete=True,
                )
                for voice in voices
                if isinstance(voice, str) and voice
            ]
        # Configured model absent from the speech catalog → no voices to
        # offer (the operator picked a non-TTS or retired slug; synth
        # attempts will surface the upstream error verbatim).
        return []


def _media_type(fmt: str) -> str:
    value = fmt.lower()
    if value == "mp3":
        return "audio/mpeg"
    if value == "ogg":
        return "audio/ogg"
    if value == "pcm":
        return "audio/pcm"
    return "audio/wav"
