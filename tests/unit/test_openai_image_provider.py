"""Unit tests for :class:`OpenAIImageProvider`.

We pin httpx with ``MockTransport`` (no real network) so we can
assert on the request payload AND on how the provider handles each
documented response shape: happy path, HTTP error, empty data, no
``b64_json``, network timeout.
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
    ImageTokenUsage,
    ImageTimeoutError,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.image.openai_provider import (
    ASPECT_TO_SIZE,
    OpenAIImageProvider,
)


_PNG = b"\x89PNG\r\n\x1a\n" + b"fakepngbytes"


def _character(
    *, appearance: str = "long black hair, red ribbon",
    emotion: str = "calm",
    intent: str | None = "reading",
    visual_subject_type: str = "auto",
) -> Character:
    state = CharacterState(
        emotion=emotion, affection=50, fatigue=20, trust=50, energy=60,
        current_intent=intent,
    )
    return Character(
        id="c1", name="Yui", summary="", personality=[], interests=[],
        speaking_style="", boundaries=[], state=state,
        appearance=appearance,
        gender_identity="非二元",
        third_person_pronoun="TA",
        visual_gender_presentation="androgynous teen",
        visual_subject_type=visual_subject_type,
    )


def _provider(
    handler: Any, *, quality: str = "medium", timeout: float = 30.0,
) -> OpenAIImageProvider:
    """Build a provider whose AsyncClient is force-routed through the
    given MockTransport handler. We monkeypatch ``httpx.AsyncClient``
    rather than threading a transport kwarg through the public ctor
    so test plumbing never bleeds into production signatures."""
    transport = httpx.MockTransport(handler)
    original_init = httpx.AsyncClient.__init__

    def patched(self: httpx.AsyncClient, **kwargs: Any) -> None:
        kwargs["transport"] = transport
        original_init(self, **kwargs)

    httpx.AsyncClient.__init__ = patched  # type: ignore[method-assign]

    provider = OpenAIImageProvider(
        api_key="sk-test", quality=quality, timeout_seconds=timeout,
    )

    # Caller is responsible for restoring after the test; we attach the
    # restore handle so the test fixture can wrap in try/finally.
    provider._restore_httpx = lambda: (  # type: ignore[attr-defined]
        setattr(httpx.AsyncClient, "__init__", original_init)
    )
    return provider


@pytest.fixture
def restore_httpx():
    original_init = httpx.AsyncClient.__init__
    yield
    httpx.AsyncClient.__init__ = original_init  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_happy_path_returns_decoded_bytes(restore_httpx: None) -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={
            "data": [{"b64_json": base64.b64encode(_PNG).decode()}],
        })

    provider = _provider(handler)
    images = await provider.generate(
        character=_character(), positive="cafe, warm light",
        aspect="portrait", batch=1,
    )

    assert images == [_PNG]
    assert captured["url"] == "https://api.openai.com/v1/images/generations"
    assert captured["auth"] == "Bearer sk-test"
    body = captured["body"]
    assert body["model"] == "gpt-image-2"
    assert body["size"] == ASPECT_TO_SIZE["portrait"]
    assert body["quality"] == "medium"
    assert body["n"] == 1
    # Prompt should layer appearance + mood + intent + scene.
    prompt = body["prompt"]
    assert "long black hair" in prompt
    assert "Character gender identity: 非二元" in prompt
    assert "Visual gender presentation: androgynous teen" in prompt
    assert "calm" in prompt
    assert "reading" in prompt
    assert "cafe, warm light" in prompt


@pytest.mark.asyncio
async def test_happy_path_captures_gpt_image_token_usage(
    restore_httpx: None,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "data": [{"b64_json": base64.b64encode(_PNG).decode()}],
            "usage": {
                "input_tokens": 120,
                "input_tokens_details": {
                    "text_tokens": 20,
                    "image_tokens": 100,
                },
                "output_tokens": 300,
                "total_tokens": 420,
            },
        })

    provider = _provider(handler)
    images = await provider.generate(
        character=_character(), positive="cafe, warm light",
    )

    assert images == [_PNG]
    assert provider.provider_id == "openai"
    assert provider.last_model_id == "gpt-image-2"
    assert provider.last_usage == ImageTokenUsage(
        input_tokens=120,
        input_text_tokens=20,
        input_image_tokens=100,
        output_tokens=300,
        output_image_tokens=300,
        total_tokens=420,
        estimated=False,
    )


@pytest.mark.asyncio
async def test_non_human_animal_prompt_includes_body_plan_rules(
    restore_httpx: None,
) -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={
            "data": [{"b64_json": base64.b64encode(_PNG).decode()}],
        })

    provider = _provider(handler)
    await provider.generate(
        character=_character(
            appearance="一隻短毛橘貓，四足姿態，圓眼睛",
            visual_subject_type="animal",
        ),
        positive="窗台上的午後陽光",
    )

    prompt = captured["body"]["prompt"]
    assert "Visual subject type: non-human animal." in prompt
    assert "Species/body plan: domestic cat." in prompt
    assert "Do NOT anthropomorphize" in prompt
    assert "no human face" in prompt
    assert "Follow Visual subject type/body-plan rules" in prompt
    assert "窗台上的午後陽光" in prompt


@pytest.mark.asyncio
async def test_aspect_landscape_maps_to_landscape_size(
    restore_httpx: None,
) -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={
            "data": [{"b64_json": base64.b64encode(_PNG).decode()}],
        })

    provider = _provider(handler)
    await provider.generate(
        character=_character(), positive="forest", aspect="landscape",
    )
    assert captured["body"]["size"] == ASPECT_TO_SIZE["landscape"]


@pytest.mark.asyncio
async def test_batch_clamped_to_max_four(restore_httpx: None) -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={
            "data": [
                {"b64_json": base64.b64encode(_PNG).decode()} for _ in range(4)
            ],
        })

    provider = _provider(handler)
    images = await provider.generate(
        character=_character(), positive="x", batch=99,
    )
    assert captured["body"]["n"] == 4
    assert len(images) == 4


@pytest.mark.asyncio
async def test_use_runtime_state_false_strips_mood_and_intent(
    restore_httpx: None,
) -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={
            "data": [{"b64_json": base64.b64encode(_PNG).decode()}],
        })

    provider = _provider(handler)
    await provider.generate(
        character=_character(emotion="sleepy", intent="準備睡覺"),
        positive="explicit operator scene",
        use_runtime_state=False,
    )
    prompt = captured["body"]["prompt"]
    assert "sleepy" not in prompt
    assert "準備睡覺" not in prompt
    assert "explicit operator scene" in prompt


@pytest.mark.asyncio
async def test_empty_prompt_raises(restore_httpx: None) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not call upstream on empty prompt")

    provider = _provider(handler)
    with pytest.raises(ImageGenerationError):
        await provider.generate(character=_character(), positive="   ")


@pytest.mark.asyncio
async def test_http_error_maps_to_image_generation_error(
    restore_httpx: None,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={
            "error": {"message": "invalid_request: prompt too long"},
        })

    provider = _provider(handler)
    with pytest.raises(ImageGenerationError) as exc_info:
        await provider.generate(character=_character(), positive="x")
    assert "400" in str(exc_info.value)
    assert "prompt too long" in str(exc_info.value)


@pytest.mark.asyncio
async def test_http_error_logs_upstream_body(
    restore_httpx: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={
            "error": {"message": "invalid_request: prompt too long"},
        })

    caplog.set_level(
        logging.ERROR,
        logger="kokoro_link.infrastructure.image.openai_provider",
    )
    provider = _provider(handler)

    with pytest.raises(ImageGenerationError):
        await provider.generate(character=_character(), positive="x")

    assert "OpenAI image API" in caplog.text
    assert "HTTP 400" in caplog.text
    assert "prompt too long" in caplog.text
    assert "https://api.openai.com/v1/images/generations" in caplog.text


@pytest.mark.asyncio
async def test_empty_data_maps_to_no_output_error(
    restore_httpx: None,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": []})

    provider = _provider(handler)
    with pytest.raises(ImageNoOutputError):
        await provider.generate(character=_character(), positive="x")


@pytest.mark.asyncio
async def test_missing_b64_json_maps_to_no_output_error(
    restore_httpx: None,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"url": "https://x/y.png"}]})

    provider = _provider(handler)
    with pytest.raises(ImageNoOutputError):
        await provider.generate(character=_character(), positive="x")


@pytest.mark.asyncio
async def test_timeout_maps_to_image_timeout_error(
    restore_httpx: None,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    provider = _provider(handler)
    with pytest.raises(ImageTimeoutError):
        await provider.generate(character=_character(), positive="x")


def test_constructor_rejects_empty_api_key() -> None:
    with pytest.raises(ValueError):
        OpenAIImageProvider(api_key="")
