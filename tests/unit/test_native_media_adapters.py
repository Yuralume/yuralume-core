"""Native hosted media adapter tests."""

from __future__ import annotations

import base64
import json
from typing import Any

import httpx
import pytest

from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.image.gemini_provider import GeminiImageProvider
from kokoro_link.infrastructure.image.xai_provider import XAIImageProvider
from kokoro_link.infrastructure.video.google_veo_provider import (
    GoogleVeoVideoProvider,
)


_PNG = b"\x89PNG\r\n\x1a\nnative-image"
_MP4 = b"\x00\x00\x00\x18ftypmp42native-video"


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
async def test_xai_image_provider_uses_native_aspect_ratio(
    restore_httpx: None,
) -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={
            "data": [{"b64_json": base64.b64encode(_PNG).decode()}],
        })

    _patch_httpx(handler)
    provider = XAIImageProvider(
        api_key="xai-key",
        model="grok-imagine-image-quality",
    )

    images = await provider.generate(
        character=_character(),
        positive="sunlit room",
        aspect="landscape",
        batch=2,
    )

    assert images == [_PNG]
    assert captured["url"] == "https://api.x.ai/v1/images/generations"
    assert captured["auth"] == "Bearer xai-key"
    assert captured["body"]["model"] == "grok-imagine-image-quality"
    assert captured["body"]["aspect_ratio"] == "16:9"
    assert captured["body"]["response_format"] == "b64_json"
    assert captured["body"]["n"] == 2
    assert "Character gender identity: 非二元" in captured["body"]["prompt"]
    assert "Visual gender presentation: androgynous student" in captured["body"]["prompt"]


@pytest.mark.asyncio
async def test_xai_image_provider_includes_non_human_animal_body_plan(
    restore_httpx: None,
) -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={
            "data": [{"b64_json": base64.b64encode(_PNG).decode()}],
        })

    _patch_httpx(handler)
    provider = XAIImageProvider(
        api_key="xai-key",
        model="grok-imagine-image-quality",
    )

    await provider.generate(
        character=_animal_character(),
        positive="sunlit windowsill",
        aspect="portrait",
    )

    prompt = captured["body"]["prompt"]
    assert "Visual subject type: non-human animal." in prompt
    assert "Species/body plan: domestic cat." in prompt
    assert "Do NOT anthropomorphize" in prompt
    assert "human face" in prompt
    assert "sunlit windowsill" in prompt


@pytest.mark.asyncio
async def test_gemini_image_provider_parses_inline_data(
    restore_httpx: None,
) -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["api_key"] = request.headers.get("x-goog-api-key")
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={
            "candidates": [{
                "content": {
                    "parts": [{
                        "inlineData": {
                            "mimeType": "image/png",
                            "data": base64.b64encode(_PNG).decode(),
                        },
                    }],
                },
            }],
        })

    _patch_httpx(handler)
    provider = GeminiImageProvider(
        api_key="gemini-key",
        model="gemini-2.5-flash-image",
    )

    images = await provider.generate(
        character=_character(),
        positive="sunlit room",
        aspect="portrait",
    )

    assert images == [_PNG]
    assert captured["url"].endswith(
        "/models/gemini-2.5-flash-image:generateContent",
    )
    assert captured["api_key"] == "gemini-key"
    config = captured["body"]["generationConfig"]["responseFormat"]["image"]
    assert config["aspectRatio"] == "9:16"
    prompt = captured["body"]["contents"][0]["parts"][0]["text"]
    assert "Character gender identity: 非二元" in prompt
    assert "Visual gender presentation: androgynous student" in prompt


@pytest.mark.asyncio
async def test_google_veo_provider_polls_and_downloads_video(
    restore_httpx: None,
) -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/models/veo-3.1-generate-preview:predictLongRunning"):
            captured["start_body"] = json.loads(request.content.decode())
            captured["api_key"] = request.headers.get("x-goog-api-key")
            return httpx.Response(200, json={"name": "operations/op-1"})
        if url.endswith("/operations/op-1"):
            return httpx.Response(200, json={
                "done": True,
                "response": {
                    "generatedVideos": [{
                        "video": {
                            "uri": "https://generativelanguage.googleapis.com/v1beta/files/video-1",
                        },
                    }],
                },
            })
        if url.endswith("/files/video-1"):
            captured["download_api_key"] = request.headers.get("x-goog-api-key")
            return httpx.Response(200, content=_MP4)
        raise AssertionError(f"unexpected request {url}")

    _patch_httpx(handler)
    provider = GoogleVeoVideoProvider(
        api_key="gemini-key",
        model="veo-3.1-generate-preview",
        poll_interval_seconds=0.01,
    )

    video = await provider.generate(
        character=_character(),
        positive="quiet street",
        aspect="landscape",
        length_frames=96,
    )

    assert video == _MP4
    assert captured["api_key"] == "gemini-key"
    assert captured["download_api_key"] == "gemini-key"
    assert captured["start_body"]["parameters"]["aspectRatio"] == "16:9"
    assert captured["start_body"]["parameters"]["durationSeconds"] == "6"
    prompt = captured["start_body"]["instances"][0]["prompt"]
    assert "Character gender identity: 非二元" in prompt
    assert "Visual gender presentation: androgynous student" in prompt
