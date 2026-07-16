"""Google Veo native video adapter."""

from __future__ import annotations

import base64
import time
from collections.abc import Mapping
from urllib.parse import urljoin
from uuid import uuid4

import httpx

from kokoro_link.contracts.video_provider import (
    VideoGenerationError,
    VideoNoOutputError,
    VideoProviderPort,
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
    "square": "16:9",
}


class GoogleVeoVideoProvider(VideoProviderPort):
    def __init__(
        self,
        *,
        base_url: str = "https://generativelanguage.googleapis.com/v1beta",
        api_key: str,
        model: str = "veo-3.1-generate-preview",
        timeout_seconds: float = 1800.0,
        poll_interval_seconds: float = 10.0,
    ) -> None:
        if not api_key.strip():
            raise ValueError("Google Veo API api_key is required")
        if not model.strip():
            raise ValueError("Google Veo API model is required")
        self._base_url = (
            base_url or "https://generativelanguage.googleapis.com/v1beta"
        ).rstrip("/")
        self._api_key = api_key
        self._model = model
        self._timeout = timeout_seconds
        self._poll_interval = max(0.01, poll_interval_seconds)

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
            raise VideoGenerationError("Google Veo prompt is empty")
        payload = {
            "instances": [{"prompt": prompt}],
            "parameters": {
                "aspectRatio": ASPECT_TO_RATIO.get(
                    aspect,
                    ASPECT_TO_RATIO["portrait"],
                ),
                "durationSeconds": _duration_seconds(length_frames),
            },
        }
        deadline = time.monotonic() + self._timeout
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                operation = await self._start_operation(client, payload)
                name = operation.get("name")
                if not isinstance(name, str) or not name:
                    raise VideoGenerationError(
                        "Google Veo API returned operation without name",
                    )
                while True:
                    if time.monotonic() >= deadline:
                        raise VideoTimeoutError("Google Veo API timed out")
                    status = await self._get_operation(client, name)
                    if status.get("done") is True:
                        return await _video_bytes_from_operation(
                            status,
                            client=client,
                            base_url=self._base_url,
                            api_key=self._api_key,
                        )
                    await _sleep(self._poll_interval, deadline)
        except httpx.TimeoutException as exc:
            raise VideoTimeoutError("Google Veo API timed out") from exc
        except VideoGenerationError:
            raise
        except Exception as exc:
            raise VideoGenerationError(str(exc)) from exc

    async def _start_operation(
        self,
        client: httpx.AsyncClient,
        payload: dict,
    ) -> Mapping:
        response = await client.post(
            f"{self._base_url}/models/{self._model}:predictLongRunning",
            headers=self._headers(),
            json=payload,
        )
        return _json_or_raise(response, "Google Veo")

    async def _get_operation(
        self,
        client: httpx.AsyncClient,
        operation_name: str,
    ) -> Mapping:
        response = await client.get(
            urljoin(f"{self._base_url}/", operation_name.lstrip("/")),
            headers={"x-goog-api-key": self._api_key},
        )
        return _json_or_raise(response, "Google Veo operation")

    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "x-goog-api-key": self._api_key,
            "X-Request-Id": f"veo-{uuid4().hex}",
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
    return "\n".join(part for part in parts if part.strip())


def _duration_seconds(length_frames: int) -> str:
    seconds = round(max(1, int(length_frames or 81)) / 16)
    if seconds <= 4:
        return "4"
    if seconds <= 6:
        return "6"
    return "8"


async def _video_bytes_from_operation(
    data: Mapping,
    *,
    client: httpx.AsyncClient,
    base_url: str,
    api_key: str,
) -> bytes:
    error = data.get("error")
    if isinstance(error, Mapping):
        raise VideoGenerationError(
            str(error.get("message") or error),
        )
    response = data.get("response")
    if not isinstance(response, Mapping):
        raise VideoNoOutputError("Google Veo operation returned no response")
    for video in _iter_video_objects(response):
        raw = _first_str(video, "videoBytes", "video_bytes", "b64", "data")
        if raw:
            return base64.b64decode(raw)
        uri = _first_str(video, "uri", "url", "videoUri", "video_uri")
        if uri:
            return await _download_video(
                client=client,
                uri=uri,
                base_url=base_url,
                api_key=api_key,
            )
    raise VideoNoOutputError("Google Veo operation produced no video")


def _iter_video_objects(response: Mapping):
    # Official Gemini API REST shape for a completed predictLongRunning
    # operation (https://ai.google.dev/gemini-api/docs/veo — the docs' own
    # extraction path is
    # .response.generateVideoResponse.generatedSamples[0].video.uri).
    generate_video_response = response.get("generateVideoResponse")
    if isinstance(generate_video_response, Mapping):
        samples = generate_video_response.get("generatedSamples")
        if isinstance(samples, list):
            for item in samples:
                if not isinstance(item, Mapping):
                    continue
                video = item.get("video")
                if isinstance(video, Mapping):
                    yield video
                else:
                    yield item
    # Fallbacks: google-genai SDK-normalized (generatedVideos) and
    # gateway/legacy (predictions) shapes.
    generated = response.get("generatedVideos") or response.get("generated_videos")
    if isinstance(generated, list):
        for item in generated:
            if not isinstance(item, Mapping):
                continue
            video = item.get("video")
            if isinstance(video, Mapping):
                yield video
            else:
                yield item
    predictions = response.get("predictions")
    if isinstance(predictions, list):
        for prediction in predictions:
            if not isinstance(prediction, Mapping):
                continue
            video = prediction.get("video")
            if isinstance(video, Mapping):
                yield video
            else:
                yield prediction


def _first_str(data: Mapping, *keys: str) -> str:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


async def _download_video(
    *,
    client: httpx.AsyncClient,
    uri: str,
    base_url: str,
    api_key: str,
) -> bytes:
    resolved = uri if uri.startswith(("http://", "https://")) else urljoin(
        f"{base_url}/",
        uri.lstrip("/"),
    )
    # The documented download flow requires following redirects (the
    # official example is `curl -L -H "x-goog-api-key: ..."` —
    # https://ai.google.dev/gemini-api/docs/veo); httpx does not follow
    # them by default, and a 3xx body is HTML, not video bytes.
    response = await client.get(
        resolved,
        headers={"x-goog-api-key": api_key},
        follow_redirects=True,
    )
    if not response.is_success:
        raise VideoGenerationError(
            f"Google Veo video download failed: {response.status_code}",
        )
    return response.content


def _json_or_raise(response: httpx.Response, label: str) -> Mapping:
    if response.status_code >= 400:
        raise VideoGenerationError(
            f"{label} API error {response.status_code}: {response.text}",
        )
    payload = response.json()
    if not isinstance(payload, Mapping):
        raise VideoGenerationError(f"{label} API returned non-object JSON")
    return payload


async def _sleep(interval: float, deadline: float) -> None:
    import asyncio

    await asyncio.sleep(min(interval, max(0.0, deadline - time.monotonic())))
