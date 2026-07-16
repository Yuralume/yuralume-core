"""Payload-shape tests for OpenAICompatibleChatModel vision mode.

Covers:

* ``supports_vision=True`` + ``image_urls`` → user message ``content``
  becomes the OpenAI multimodal array ``[{"type": "text"}, {"type":
  "image_url"}]``.
* ``supports_vision=False`` + ``image_urls`` → images are dropped,
  payload stays on the plain-string shape so non-vision servers don't
  choke on the array form.
* ``supports_vision=True`` + empty ``image_urls`` → still plain string
  (we don't wrap single-text in an array needlessly).
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from kokoro_link.contracts.llm import ImageInputRejectedError
from kokoro_link.infrastructure.llm.openai_compatible import (
    OpenAICompatibleChatModel,
)


def _patch_transport(transport: httpx.MockTransport) -> Any:
    """Force every fresh ``httpx.AsyncClient`` to use the given mock.

    Mirrors the helper in ``test_openai_compatible_models.py`` — respx
    isn't a dependency, so we swap ``AsyncClient.__init__`` for the
    duration of the test."""
    original_init = httpx.AsyncClient.__init__

    def patched_init(self: httpx.AsyncClient, **kwargs: Any) -> None:
        kwargs["transport"] = transport
        original_init(self, **kwargs)

    class _Ctx:
        def __enter__(self) -> None:
            httpx.AsyncClient.__init__ = patched_init  # type: ignore[method-assign]

        def __exit__(self, *_: Any) -> None:
            httpx.AsyncClient.__init__ = original_init  # type: ignore[method-assign]

    return _Ctx()


def _build(*, supports_vision: bool) -> OpenAICompatibleChatModel:
    return OpenAICompatibleChatModel(
        provider_id="lmstudio",
        base_url="http://127.0.0.1:1234/v1",
        api_key=None,
        model="stub",
        supports_vision=supports_vision,
    )


def test_multimodal_payload_when_vision_and_images_present() -> None:
    model = _build(supports_vision=True)
    payload = model._build_payload(
        "describe this",
        image_urls=("https://cdn.example/a.png", "https://cdn.example/b.png"),
    )
    user_msg = payload["messages"][-1]
    assert user_msg["role"] == "user"
    content = user_msg["content"]
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "describe this"}
    assert content[1] == {
        "type": "image_url",
        "image_url": {"url": "https://cdn.example/a.png"},
    }
    assert content[2]["image_url"]["url"] == "https://cdn.example/b.png"


def test_non_vision_payload_stays_plain_string_even_with_images() -> None:
    model = _build(supports_vision=False)
    payload = model._build_payload(
        "describe this",
        image_urls=("https://cdn.example/a.png",),
    )
    user_msg = payload["messages"][-1]
    assert user_msg["content"] == "describe this"


def test_vision_with_no_images_stays_plain_string() -> None:
    model = _build(supports_vision=True)
    payload = model._build_payload("hello")
    user_msg = payload["messages"][-1]
    assert user_msg["content"] == "hello"


# ---- routing-level vision override (with_supports_vision) -------------


def test_with_supports_vision_returns_flipped_clone_leaving_base() -> None:
    base = _build(supports_vision=False)
    bound = base.with_supports_vision(True)
    assert bound is not base
    assert bound.supports_vision is True
    # Base singleton untouched — the registry instance is never mutated.
    assert base.supports_vision is False


def test_with_supports_vision_shares_models_cache_object() -> None:
    """Per-call clones must share the model-list cache with the base
    adapter so they don't re-pay the /models probe (same rationale as
    the reasoning clone)."""
    base = _build(supports_vision=False)
    base._models_cache = ["a", "b"]  # populate so the identity is non-trivial
    bound = base.with_supports_vision(True)
    assert bound._models_cache is base._models_cache


def test_with_supports_vision_shares_non_chat_model_memory() -> None:
    base = _build(supports_vision=False)
    bound = base.with_supports_vision(True)
    bound._remember_non_chat_override("embedding-model")
    # Shared set → base resolving that model also falls back to default.
    payload = base._build_payload("hi", model="embedding-model")
    assert payload["model"] == "stub"


def test_with_supports_vision_true_flips_payload_to_multimodal() -> None:
    """A text-only connection bound to vision=True emits the multimodal
    array shape for the same image_urls."""
    base = _build(supports_vision=False)
    bound = base.with_supports_vision(True)
    payload = bound._build_payload(
        "describe", image_urls=("https://cdn.example/a.png",),
    )
    content = payload["messages"][-1]["content"]
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "describe"}
    assert content[1]["image_url"]["url"] == "https://cdn.example/a.png"


def test_with_supports_vision_false_drops_images() -> None:
    base = _build(supports_vision=True)
    bound = base.with_supports_vision(False)
    payload = bound._build_payload(
        "describe", image_urls=("https://cdn.example/a.png",),
    )
    assert payload["messages"][-1]["content"] == "describe"


# ---- image-input rejection classification ----------------------------
#
# When the upstream rejects a request that carried image parts with one
# of the "shape/size/unprocessable" 4xx statuses, the adapter must raise
# the typed ``ImageInputRejectedError`` (chained from the original
# ``HTTPStatusError``) so the caller can degrade + retry without images.
# Auth / rate-limit statuses, and 4xx on image-free requests, stay bare.

_IMAGE_REJECT_BODY = {
    "error": {
        "message": "No endpoints found that support image input",
        "code": 404,
    },
}


@pytest.mark.asyncio
async def test_generate_404_with_images_raises_image_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json=_IMAGE_REJECT_BODY)

    model = _build(supports_vision=True)
    with _patch_transport(httpx.MockTransport(handler)):
        with pytest.raises(ImageInputRejectedError) as excinfo:
            await model.generate(
                "describe this",
                image_urls=("https://cdn.example/a.png",),
            )
    assert excinfo.value.status_code == 404
    assert "No endpoints found" in excinfo.value.body
    # Chained from the underlying HTTP error for diagnostics.
    assert isinstance(excinfo.value.__cause__, httpx.HTTPStatusError)


@pytest.mark.asyncio
async def test_generate_404_without_images_raises_plain_http_error() -> None:
    """Same status, but no image parts in the payload → the adapter must
    NOT reclassify. A bare ``HTTPStatusError`` propagates."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json=_IMAGE_REJECT_BODY)

    model = _build(supports_vision=True)
    with _patch_transport(httpx.MockTransport(handler)):
        with pytest.raises(httpx.HTTPStatusError):
            await model.generate("describe this")  # no images


@pytest.mark.asyncio
async def test_generate_401_with_images_not_classified() -> None:
    """401 is auth, never image rejection — even with images attached it
    must stay a bare ``HTTPStatusError``."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "unauthorized"}})

    model = _build(supports_vision=True)
    with _patch_transport(httpx.MockTransport(handler)):
        with pytest.raises(httpx.HTTPStatusError) as excinfo:
            await model.generate(
                "describe this",
                image_urls=("https://cdn.example/a.png",),
            )
    assert not isinstance(excinfo.value, ImageInputRejectedError)


@pytest.mark.asyncio
async def test_generate_stream_4xx_with_images_raises_image_rejected() -> None:
    """The streaming path surfaces the 4xx before the first token — it
    must classify image rejection identically to the non-stream path."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json=_IMAGE_REJECT_BODY)

    model = _build(supports_vision=True)
    with _patch_transport(httpx.MockTransport(handler)):
        with pytest.raises(ImageInputRejectedError) as excinfo:
            async for _chunk in model.generate_stream(
                "describe this",
                image_urls=("https://cdn.example/a.png",),
            ):
                pass
    assert excinfo.value.status_code == 422
    assert isinstance(excinfo.value.__cause__, httpx.HTTPStatusError)
