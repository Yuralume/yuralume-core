"""External media capability adapter tests.

The core app should only speak stable capability APIs here: normalized
image/video generation and the Yuralume TTS voice/synthesize contract. Provider
specific details such as ComfyUI workflows or GPT-SoVITS paths belong behind
those services, not in this project.
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Any

import httpx
import pytest

from kokoro_link.contracts.image_provider import (
    ImageGenerationError,
    ImageNoOutputError,
)
from kokoro_link.contracts.tts import TTSRequest
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.image.external_api_provider import (
    ExternalImageApiProvider,
)
from kokoro_link.infrastructure.tts.external_api import ExternalTTSAdapter
from kokoro_link.infrastructure.video.external_api_provider import (
    ExternalVideoApiProvider,
)


_PNG = b"\x89PNG\r\n\x1a\nexternal-image"
_MP4 = b"\x00\x00\x00\x18ftypmp42external-video"
_WAV = b"RIFF....WAVEexternal-audio"


def _character() -> Character:
    return Character(
        id="c1",
        name="Aiko",
        summary="quiet student",
        personality=[],
        interests=[],
        speaking_style="",
        boundaries=[],
        appearance="black hair, blue eyes",
        gender_identity="非二元",
        third_person_pronoun="TA",
        visual_gender_presentation="androgynous student",
        state=CharacterState(
            emotion="calm",
            affection=50,
            fatigue=10,
            trust=50,
            energy=80,
            current_intent="reading",
        ),
    )


def _animal_character() -> Character:
    return Character(
        id="c-cat",
        name="Mochi",
        summary="balcony cat",
        personality=[],
        interests=[],
        speaking_style="",
        boundaries=[],
        appearance="一隻短毛橘貓，四足姿態，圓眼睛",
        gender_identity="非二元",
        third_person_pronoun="TA",
        visual_gender_presentation="可愛寵物貓",
        visual_subject_type="animal",
        state=CharacterState(
            emotion="calm",
            affection=50,
            fatigue=10,
            trust=50,
            energy=80,
        ),
    )


@pytest.fixture
def restore_httpx():
    original_init = httpx.AsyncClient.__init__
    yield
    httpx.AsyncClient.__init__ = original_init  # type: ignore[method-assign]


def _patch_httpx(handler: Any) -> None:
    transport = httpx.MockTransport(handler)
    original_init = httpx.AsyncClient.__init__

    def patched(self: httpx.AsyncClient, **kwargs: Any) -> None:
        kwargs["transport"] = transport
        original_init(self, **kwargs)

    httpx.AsyncClient.__init__ = patched  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_external_image_api_sends_gateway_request(
    restore_httpx: None,
) -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["request_id"] = request.headers.get("x-request-id")
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={
            "data": [{"b64_json": base64.b64encode(_PNG).decode()}],
        })

    _patch_httpx(handler)
    provider = ExternalImageApiProvider(
        base_url="https://gateway.example/v1",
        api_key="image-token",
        model="gpt-image2",
        timeout_seconds=30,
    )

    images = await provider.generate(
        character=_character(),
        positive="sunlit room",
        aspect="square",
        batch=2,
    )

    assert images == [_PNG]
    assert captured["url"] == "https://gateway.example/v1/images/generations"
    assert captured["auth"] == "Bearer image-token"
    assert captured["request_id"]
    assert captured["body"]["model"] == "gpt-image2"
    assert captured["body"]["prompt"].startswith("Character: Aiko")
    assert "Character gender identity: 非二元" in captured["body"]["prompt"]
    assert "Visual gender presentation: androgynous student" in captured["body"]["prompt"]
    assert "sunlit room" in captured["body"]["prompt"]
    assert captured["body"]["size"] == "1024x1024"
    assert captured["body"]["n"] == 2


@pytest.mark.asyncio
async def test_external_image_api_includes_non_human_animal_body_plan(
    restore_httpx: None,
) -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={
            "data": [{"b64_json": base64.b64encode(_PNG).decode()}],
        })

    _patch_httpx(handler)
    provider = ExternalImageApiProvider(
        base_url="https://gateway.example/v1",
        api_key="image-token",
        model="gpt-image2",
    )

    await provider.generate(
        character=_animal_character(),
        positive="sunlit windowsill",
    )

    prompt = captured["body"]["prompt"]
    assert "Visual subject type: non-human animal." in prompt
    assert "Species/body plan: domestic cat." in prompt
    assert "Do NOT anthropomorphize" in prompt
    assert "human face" in prompt
    assert "sunlit windowsill" in prompt


def test_external_image_api_rejects_unknown_provider_without_adapter() -> None:
    with pytest.raises(ValueError, match="dedicated native adapter"):
        ExternalImageApiProvider(
            base_url="https://native.example/v1",
            api_key="image-token",
            model="unknown-image",
            provider="unknown_native",
        )


def test_external_video_api_rejects_unknown_provider_without_adapter() -> None:
    with pytest.raises(ValueError, match="dedicated native adapter"):
        ExternalVideoApiProvider(
            base_url="https://native.example/v1",
            api_key="video-token",
            model="unknown-video",
            provider="unknown_native",
        )


@pytest.mark.asyncio
async def test_external_image_api_downloads_artifact_url(
    restore_httpx: None,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/v1/images/generations"):
            return httpx.Response(200, json={
                "data": [{"url": "https://gateway.example/v1/artifacts/a.png"}],
            })
        if str(request.url) == "https://gateway.example/v1/artifacts/a.png":
            return httpx.Response(200, content=_PNG, headers={
                "content-type": "image/png",
            })
        raise AssertionError(f"unexpected request {request.url}")

    _patch_httpx(handler)
    provider = ExternalImageApiProvider(
        base_url="https://gateway.example/v1",
        api_key="image-token",
        model="yuralume-anime",
    )

    assert await provider.generate(character=_character(), positive="x") == [_PNG]


@pytest.mark.asyncio
async def test_external_image_api_empty_output_raises(
    restore_httpx: None,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": []})

    _patch_httpx(handler)
    provider = ExternalImageApiProvider(
        base_url="https://gateway.example/v1",
        api_key="image-token",
        model="gpt-image2",
    )

    with pytest.raises(ImageNoOutputError):
        await provider.generate(character=_character(), positive="x")


@pytest.mark.asyncio
async def test_external_image_api_http_error_logs_upstream_body(
    restore_httpx: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={
            "error": {"message": "prompt violates provider policy"},
        })

    _patch_httpx(handler)
    caplog.set_level(
        logging.ERROR,
        logger="kokoro_link.infrastructure.image.external_api_provider",
    )
    provider = ExternalImageApiProvider(
        base_url="https://gateway.example/v1",
        api_key="image-token",
        model="gpt-image2",
    )

    with pytest.raises(ImageGenerationError):
        await provider.generate(character=_character(), positive="x")

    assert "image API" in caplog.text
    assert "HTTP 422" in caplog.text
    assert "provider policy" in caplog.text
    assert "https://gateway.example/v1/images/generations" in caplog.text


@pytest.mark.asyncio
async def test_external_video_api_downloads_first_video_url(
    restore_httpx: None,
) -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/v1/videos/generations"):
            captured["body"] = json.loads(request.content.decode())
            return httpx.Response(200, json={
                "id": "job_1",
                "data": [{"url": "https://gateway.example/v1/artifacts/v.mp4"}],
            })
        if str(request.url) == "https://gateway.example/v1/artifacts/v.mp4":
            return httpx.Response(200, content=_MP4, headers={
                "content-type": "video/mp4",
            })
        raise AssertionError(f"unexpected request {request.url}")

    _patch_httpx(handler)
    provider = ExternalVideoApiProvider(
        base_url="https://gateway.example/v1",
        api_key="video-token",
        model="veo3",
        timeout_seconds=300,
    )

    video = await provider.generate(
        character=_character(),
        positive="a quiet street",
        aspect="landscape",
        length_frames=96,
    )

    assert video == _MP4
    assert captured["body"]["model"] == "veo3"
    assert "Character gender identity: 非二元" in captured["body"]["prompt"]
    assert "Visual gender presentation: androgynous student" in captured["body"]["prompt"]
    assert captured["body"]["aspect_ratio"] == "16:9"
    assert captured["body"]["duration_seconds"] == 6


@pytest.mark.asyncio
async def test_external_tts_adapter_lists_voices_and_synthesizes(
    restore_httpx: None,
) -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/v1/voices"):
            return httpx.Response(200, json={
                "voices": [
                    {"id": "marin", "label": "Marin", "is_complete": True},
                ],
            })
        if str(request.url).endswith("/v1/tts/synthesize"):
            captured["auth"] = request.headers.get("authorization")
            captured["body"] = json.loads(request.content.decode())
            return httpx.Response(200, content=_WAV, headers={
                "content-type": "audio/wav",
            })
        raise AssertionError(f"unexpected request {request.url}")

    _patch_httpx(handler)
    adapter = ExternalTTSAdapter(
        base_url="https://gateway.example/v1",
        api_key="tts-token",
        default_voice_id="marin",
        timeout_seconds=30,
    )

    voices = await adapter.list_voices()
    result = await adapter.synthesize(TTSRequest(text="hello", voice_id=""))

    assert voices[0].id == "marin"
    assert result.audio == _WAV
    assert result.media_type == "audio/wav"
    assert captured["auth"] == "Bearer tts-token"
    assert captured["body"]["voice_id"] == "marin"
    assert captured["body"]["text"] == "hello"
