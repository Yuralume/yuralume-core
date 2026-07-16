"""Unit tests for :class:`OpenRouterImageProvider`.

We pin httpx with ``MockTransport`` (no real network) so we can assert
on the request path/payload AND on how the provider handles each
documented response shape. The load-bearing distinction versus the
OpenAI adapter is the endpoint path: OpenRouter posts ``/api/v1/images``
(NOT ``/images/generations``) and returns the OpenAI-Images
``data[].b64_json`` shape (verified 2026-07-05).
"""

from __future__ import annotations

import base64
import json
from typing import Any

import httpx
import pytest

from kokoro_link.contracts.image_provider import (
    ImageGenerationError,
    ImageNoOutputError,
    ImageTimeoutError,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.image.openrouter_provider import (
    OpenRouterImageProvider,
)


_PNG = b"\x89PNG\r\n\x1a\n" + b"fakepngbytes"


def _character(*, appearance: str = "long black hair, red ribbon") -> Character:
    state = CharacterState(
        emotion="calm", affection=50, fatigue=20, trust=50, energy=60,
        current_intent="reading",
    )
    return Character(
        id="c1", name="Yui", summary="", personality=[], interests=[],
        speaking_style="", boundaries=[], state=state,
        appearance=appearance,
    )


@pytest.fixture
def restore_httpx():
    original_init = httpx.AsyncClient.__init__
    yield
    httpx.AsyncClient.__init__ = original_init  # type: ignore[method-assign]


def _provider(handler: Any, *, model: str = "black-forest-labs/flux.2-pro") -> OpenRouterImageProvider:
    transport = httpx.MockTransport(handler)
    original_init = httpx.AsyncClient.__init__

    def patched(self: httpx.AsyncClient, **kwargs: Any) -> None:
        kwargs["transport"] = transport
        original_init(self, **kwargs)

    httpx.AsyncClient.__init__ = patched  # type: ignore[method-assign]
    return OpenRouterImageProvider(
        api_key="sk-or-test", model=model, timeout_seconds=30.0,
    )


@pytest.mark.asyncio
async def test_happy_path_posts_images_endpoint(restore_httpx: None) -> None:
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
        character=_character(), positive="cafe, warm light", batch=1,
    )

    assert images == [_PNG]
    # The load-bearing assertion: OpenRouter path is /images, not
    # /images/generations.
    assert captured["url"] == "https://openrouter.ai/api/v1/images"
    assert captured["auth"] == "Bearer sk-or-test"
    body = captured["body"]
    assert body["model"] == "black-forest-labs/flux.2-pro"
    assert body["n"] == 1
    assert "long black hair" in body["prompt"]
    assert "cafe, warm light" in body["prompt"]
    assert provider.provider_id == "openrouter"


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
    images = await provider.generate(character=_character(), positive="x", batch=99)
    assert captured["body"]["n"] == 4
    assert len(images) == 4


@pytest.mark.asyncio
async def test_n_shortfall_tops_up_with_single_image_requests(
    restore_httpx: None,
) -> None:
    """OpenRouter providers may clamp ``n`` to their supported subset and
    silently return fewer images (flux.2-family caps n at 1 — see
    https://openrouter.ai/docs/guides/overview/multimodal/image-generation
    'Providers clamp to their supported subset'). The adapter must react
    to the observed shortfall with n=1 top-up requests — no per-model
    capability table."""
    requested_ns: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        requested_ns.append(body["n"])
        # Always return a single image regardless of n — the clamp.
        return httpx.Response(200, json={
            "data": [{"b64_json": base64.b64encode(_PNG).decode()}],
        })

    provider = _provider(handler)
    images = await provider.generate(character=_character(), positive="x", batch=3)

    assert len(images) == 3
    assert requested_ns == [3, 1, 1]


@pytest.mark.asyncio
async def test_top_up_failure_returns_partial_batch(restore_httpx: None) -> None:
    """A failed top-up must not throw away already-billed images."""
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] == 1:
            return httpx.Response(200, json={
                "data": [{"b64_json": base64.b64encode(_PNG).decode()}],
            })
        return httpx.Response(429, text="rate limited")

    provider = _provider(handler)
    images = await provider.generate(character=_character(), positive="x", batch=2)

    assert images == [_PNG]
    assert calls["count"] == 2


@pytest.mark.asyncio
async def test_url_item_is_downloaded(restore_httpx: None) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/images"):
            return httpx.Response(200, json={
                "data": [{"url": "https://cdn.example/out.png"}],
            })
        return httpx.Response(200, content=_PNG)

    provider = _provider(handler)
    images = await provider.generate(character=_character(), positive="x")
    assert images == [_PNG]


@pytest.mark.asyncio
async def test_empty_prompt_raises(restore_httpx: None) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not call upstream on empty prompt")

    # A character with no name/appearance/scene yields an empty prompt.
    empty = Character(
        id="c1", name="", summary="", personality=[], interests=[],
        speaking_style="", boundaries=[],
        state=CharacterState(
            emotion="", affection=50, fatigue=20, trust=50, energy=60,
        ),
        appearance="",
    )
    provider = _provider(handler)
    with pytest.raises(ImageGenerationError):
        await provider.generate(character=empty, positive="   ", use_runtime_state=False)


@pytest.mark.asyncio
async def test_http_error_maps_to_image_generation_error(restore_httpx: None) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="bad model")

    provider = _provider(handler)
    with pytest.raises(ImageGenerationError) as exc_info:
        await provider.generate(character=_character(), positive="x")
    assert "400" in str(exc_info.value)


@pytest.mark.asyncio
async def test_empty_data_maps_to_no_output_error(restore_httpx: None) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": []})

    provider = _provider(handler)
    with pytest.raises(ImageNoOutputError):
        await provider.generate(character=_character(), positive="x")


@pytest.mark.asyncio
async def test_timeout_maps_to_image_timeout_error(restore_httpx: None) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    provider = _provider(handler)
    with pytest.raises(ImageTimeoutError):
        await provider.generate(character=_character(), positive="x")


def test_constructor_rejects_empty_api_key() -> None:
    with pytest.raises(ValueError):
        OpenRouterImageProvider(api_key="", model="flux")


def test_constructor_rejects_empty_model() -> None:
    with pytest.raises(ValueError):
        OpenRouterImageProvider(api_key="sk", model="")
