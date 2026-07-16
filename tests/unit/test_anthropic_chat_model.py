"""BDD: AnthropicChatModel native /v1/messages adapter.

Covers the bits that differ from the OpenAI-compatible path:
1. Auth header shape (``x-api-key`` + ``anthropic-version``)
2. Top-level ``system`` + mandatory ``max_tokens``
3. Image content-blocks with ``source.type="url"``
4. SSE ``content_block_delta`` stream parsing
5. ``list_models()`` fallback when the API errors
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from kokoro_link.contracts.llm import ReasoningOverrides
from kokoro_link.infrastructure.llm.anthropic import AnthropicChatModel


def _build(model: str = "claude-sonnet-4-5") -> AnthropicChatModel:
    return AnthropicChatModel(
        api_key="test-key",
        base_url="https://api.anthropic.com",
        model=model,
        max_tokens=1024,
    )


def _patch_transport(transport: httpx.MockTransport) -> Any:
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


# ---- payload shape ---------------------------------------------------


def test_requires_api_key() -> None:
    with pytest.raises(ValueError):
        AnthropicChatModel(api_key="")


def test_payload_has_top_level_system_and_max_tokens() -> None:
    model = _build()
    payload = model._build_payload("hello")
    assert payload["model"] == "claude-sonnet-4-5"
    assert payload["max_tokens"] == 1024
    assert isinstance(payload["system"], str) and payload["system"]
    assert payload["messages"] == [
        {"role": "user", "content": "hello"},
    ]
    assert "stream" not in payload


def test_payload_stream_flag() -> None:
    payload = _build()._build_payload("hi", stream=True)
    assert payload["stream"] is True


def test_explicit_model_override_wins() -> None:
    payload = _build("default")._build_payload("hi", model="claude-opus-4-7")
    assert payload["model"] == "claude-opus-4-7"


def test_empty_override_falls_back_to_default() -> None:
    payload = _build("default")._build_payload("hi", model="  ")
    assert payload["model"] == "default"


def test_image_urls_become_content_blocks() -> None:
    model = _build()
    payload = model._build_payload("describe", image_urls=["https://x/1.png"])
    content = payload["messages"][0]["content"]
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "describe"}
    assert content[1] == {
        "type": "image",
        "source": {"type": "url", "url": "https://x/1.png"},
    }


def test_data_url_split_into_anthropic_base64_source() -> None:
    """Regression: ``ChatService._to_vision_url`` prefers inlining local
    uploads as ``data:image/...;base64,...`` URLs. The Anthropic API
    doesn't accept those as ``source.url`` — it needs the native
    ``{type: "base64", media_type, data}`` shape. Test that we split
    correctly so cross-turn image carry-over actually reaches the
    model instead of getting rejected with 400 invalid_request.
    """
    model = _build()
    data_url = "data:image/png;base64,AAAAABBBBB"
    payload = model._build_payload("describe", image_urls=[data_url])
    content = payload["messages"][0]["content"]
    assert isinstance(content, list)
    assert content[1] == {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": "AAAAABBBBB",
        },
    }


def test_data_url_with_jpeg_media_type_preserved() -> None:
    model = _build()
    payload = model._build_payload(
        "describe",
        image_urls=["data:image/jpeg;base64,ZZZZ"],
    )
    content = payload["messages"][0]["content"]
    assert content[1]["source"]["media_type"] == "image/jpeg"
    assert content[1]["source"]["data"] == "ZZZZ"


def test_http_url_passes_through_as_url_source() -> None:
    """Regular HTTP URLs (public CDN, TG / LINE hosted media) stay on
    the ``type: url`` path — only ``data:`` URLs get the base64 split.
    """
    model = _build()
    payload = model._build_payload(
        "describe",
        image_urls=["https://cdn.example.com/pic.jpg"],
    )
    content = payload["messages"][0]["content"]
    assert content[1]["source"] == {
        "type": "url", "url": "https://cdn.example.com/pic.jpg",
    }


def test_malformed_data_url_is_dropped_rather_than_crashing() -> None:
    """A ``data:`` URL without the expected ``;base64,`` separator would
    otherwise land as invalid payload and 400 the whole turn. We drop
    the offending image and keep the text so the reply still goes
    through."""
    model = _build()
    payload = model._build_payload(
        "describe",
        image_urls=["data:image/png,not-actually-base64"],
    )
    content = payload["messages"][0]["content"]
    # Only the text block survives; the broken image got filtered.
    assert len(content) == 1
    assert content[0]["type"] == "text"


def test_images_skipped_when_vision_disabled() -> None:
    model = AnthropicChatModel(
        api_key="k", supports_vision=False,
    )
    payload = model._build_payload("hi", image_urls=["https://x/1.png"])
    # Falls back to plain string content — no image block injected.
    assert payload["messages"][0]["content"] == "hi"


# ---- extended thinking -----------------------------------------------


def test_thinking_block_absent_by_default() -> None:
    """Regression pin: no thinking budget → no ``thinking`` key, payload
    identical to before extended-thinking support landed."""
    payload = _build()._build_payload("hi")
    assert "thinking" not in payload


def test_thinking_budget_emits_enabled_block() -> None:
    model = AnthropicChatModel(
        api_key="k",
        model="claude-sonnet-4-5",
        max_tokens=8192,
        thinking_budget_tokens=4096,
    )
    payload = model._build_payload("hi")
    assert payload["thinking"] == {
        "type": "enabled",
        "budget_tokens": 4096,
    }


# ---- routing-level reasoning override (with_reasoning_overrides) -----


def test_reasoning_override_sets_thinking_budget() -> None:
    bound = _build().with_reasoning_overrides(
        ReasoningOverrides(thinking_budget_tokens=2048),
    )
    payload = bound._build_payload("hi")
    assert payload["thinking"] == {
        "type": "enabled",
        "budget_tokens": 2048,
    }


def test_reasoning_override_replaces_connection_budget() -> None:
    """Whole-trio replacement: an override without a budget turns the
    connection-level extended thinking OFF for that route."""
    base = AnthropicChatModel(
        api_key="k",
        model="claude-sonnet-4-5",
        max_tokens=8192,
        thinking_budget_tokens=4096,
    )
    bound = base.with_reasoning_overrides(
        ReasoningOverrides(reasoning_effort="high"),
    )
    payload = bound._build_payload("hi")
    assert "thinking" not in payload
    # The base adapter keeps its connection-level posture.
    assert base._build_payload("hi")["thinking"]["budget_tokens"] == 4096


def test_reasoning_override_disable_wins_over_budget() -> None:
    """A contradictory override (disable + budget) resolves to OFF —
    conservative reading of operator intent."""
    bound = _build().with_reasoning_overrides(
        ReasoningOverrides(disable_reasoning=True, thinking_budget_tokens=2048),
    )
    payload = bound._build_payload("hi")
    assert "thinking" not in payload


@pytest.mark.asyncio
async def test_stream_drops_thinking_deltas() -> None:
    """Extended thinking arrives as ``thinking_delta`` events; the stream
    loop only forwards ``text_delta`` so the reasoning never reaches the
    reply. Pin that free filtering so a future refactor can't regress it.
    """
    sse = (
        'data: {"type":"message_start"}\n\n'
        'data: {"type":"content_block_delta","delta":'
        '{"type":"thinking_delta","thinking":"internal reasoning here"}}\n\n'
        'data: {"type":"content_block_delta","delta":'
        '{"type":"text_delta","text":"Hel"}}\n\n'
        'data: {"type":"content_block_delta","delta":'
        '{"type":"thinking_delta","thinking":"more reasoning"}}\n\n'
        'data: {"type":"content_block_delta","delta":'
        '{"type":"text_delta","text":"lo"}}\n\n'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=sse.encode("utf-8"),
            headers={"content-type": "text/event-stream"},
        )

    model = AnthropicChatModel(
        api_key="k",
        max_tokens=8192,
        thinking_budget_tokens=4096,
    )
    chunks: list[str] = []
    with _patch_transport(httpx.MockTransport(handler)):
        async for piece in model.generate_stream("hi"):
            chunks.append(piece)

    joined = "".join(chunks)
    assert joined == "Hello"
    assert "reasoning" not in joined


def test_headers_use_anthropic_auth_shape() -> None:
    model = _build()
    headers = model._headers()
    assert headers["x-api-key"] == "test-key"
    assert headers["anthropic-version"] == "2023-06-01"
    assert headers["content-type"] == "application/json"
    assert "authorization" not in {k.lower() for k in headers}


# ---- generate --------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_extracts_text_blocks() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        return httpx.Response(200, json={
            "content": [
                {"type": "text", "text": "Hel"},
                {"type": "tool_use", "name": "t", "input": {}},
                {"type": "text", "text": "lo"},
            ],
        })

    model = _build()
    with _patch_transport(httpx.MockTransport(handler)):
        out = await model.generate("hi")

    assert out == "Hello"
    assert captured["url"].endswith("/v1/messages")
    assert captured["headers"]["x-api-key"] == "test-key"


@pytest.mark.asyncio
async def test_generate_raises_on_http_error_with_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text='{"error":"bad"}')

    model = _build()
    with _patch_transport(httpx.MockTransport(handler)):
        with pytest.raises(httpx.HTTPStatusError):
            await model.generate("hi")


# ---- streaming -------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_parses_content_block_deltas() -> None:
    sse = (
        'event: message_start\n'
        'data: {"type":"message_start"}\n\n'
        'event: content_block_delta\n'
        'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"Hel"}}\n\n'
        'event: ping\n'
        'data: {"type":"ping"}\n\n'
        'event: content_block_delta\n'
        'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"lo"}}\n\n'
        'event: message_stop\n'
        'data: {"type":"message_stop"}\n\n'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=sse.encode("utf-8"),
            headers={"content-type": "text/event-stream"},
        )

    model = _build()
    chunks: list[str] = []
    with _patch_transport(httpx.MockTransport(handler)):
        async for piece in model.generate_stream("hi"):
            chunks.append(piece)

    assert "".join(chunks) == "Hello"


@pytest.mark.asyncio
async def test_stream_raises_on_http_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="unauth")

    model = _build()
    with _patch_transport(httpx.MockTransport(handler)):
        with pytest.raises(httpx.HTTPStatusError):
            async for _ in model.generate_stream("hi"):
                pass


# ---- list_models -----------------------------------------------------


@pytest.mark.asyncio
async def test_list_models_parses_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "data": [
                {"id": "claude-opus-4-7"},
                {"id": "claude-sonnet-4-5"},
            ],
        })

    model = _build("claude-sonnet-4-5")
    with _patch_transport(httpx.MockTransport(handler)):
        models = await model.list_models()

    assert "claude-opus-4-7" in models
    assert "claude-sonnet-4-5" in models


@pytest.mark.asyncio
async def test_list_models_falls_back_on_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    model = _build("claude-sonnet-4-5")
    with _patch_transport(httpx.MockTransport(handler)):
        models = await model.list_models()

    assert models == ["claude-sonnet-4-5"]


@pytest.mark.asyncio
async def test_list_models_inserts_default_when_missing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "data": [{"id": "claude-haiku-4-5"}],
        })

    model = _build("claude-sonnet-4-5")
    with _patch_transport(httpx.MockTransport(handler)):
        models = await model.list_models()

    assert models[0] == "claude-sonnet-4-5"
    assert "claude-haiku-4-5" in models
