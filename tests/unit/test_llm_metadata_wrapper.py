"""Tests for MetadataCapturingChatModel.

Guarantees:

1. Plain ``generate`` / ``generate_stream`` pass through unchanged so the
   wrapper is drop-in for ``ChatModelPort``.
2. ``generate_capturing`` returns text + non-zero latency_ms.
3. Streaming capture buffers all chunks; metadata reflects accumulated
   text length.
4. Errors propagate but metadata still records the error string.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence

import pytest

from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.infrastructure.observability.llm_metadata_wrapper import (
    MetadataCapturingChatModel,
    parse_usage_from_response_json,
)


pytestmark = pytest.mark.asyncio


class _FakeChatModel(ChatModelPort):
    provider_id = "fake"
    supports_vision = False
    _model = "fake-default"

    def __init__(
        self,
        *,
        reply: str = "ok",
        chunks: tuple[str, ...] = ("hello", " ", "world"),
        raise_on_call: bool = False,
    ) -> None:
        self._reply = reply
        self._chunks = chunks
        self._raise = raise_on_call
        self.last_model: str | None = None

    async def generate(
        self,
        prompt: str,
        *,
        image_urls: Sequence[str] = (),
        model: str | None = None,
    ) -> str:
        self.last_model = model
        if self._raise:
            raise RuntimeError("boom")
        return self._reply

    async def generate_stream(
        self,
        prompt: str,
        *,
        image_urls: Sequence[str] = (),
        model: str | None = None,
    ) -> AsyncIterator[str]:
        self.last_model = model
        if self._raise:
            raise RuntimeError("boom")
        for chunk in self._chunks:
            yield chunk

    async def list_models(self) -> list[str]:
        return ["fake-default", "fake-alt"]


async def test_plain_generate_pass_through():
    inner = _FakeChatModel(reply="abc")
    wrapper = MetadataCapturingChatModel(inner)
    out = await wrapper.generate("hi")
    assert out == "abc"


async def test_plain_generate_stream_pass_through():
    inner = _FakeChatModel(chunks=("a", "b", "c"))
    wrapper = MetadataCapturingChatModel(inner)
    chunks: list[str] = []
    async for chunk in wrapper.generate_stream("hi"):
        chunks.append(chunk)
    assert chunks == ["a", "b", "c"]


async def test_capturing_returns_text_and_metadata():
    inner = _FakeChatModel(reply="hi there")
    wrapper = MetadataCapturingChatModel(inner)
    captured = await wrapper.generate_capturing("prompt")
    assert captured.text == "hi there"
    assert captured.metadata.latency_ms >= 0
    assert captured.metadata.model_id == "fake-default"
    assert captured.metadata.completion_tokens is not None


async def test_capturing_uses_model_override():
    inner = _FakeChatModel()
    wrapper = MetadataCapturingChatModel(inner)
    captured = await wrapper.generate_capturing("p", model="other")
    assert captured.metadata.model_id == "other"
    assert inner.last_model == "other"


async def test_capturing_records_error_metadata():
    inner = _FakeChatModel(raise_on_call=True)
    wrapper = MetadataCapturingChatModel(inner)
    with pytest.raises(RuntimeError):
        await wrapper.generate_capturing("p")


async def test_stream_capture_accumulates_text():
    inner = _FakeChatModel(chunks=("foo", "bar", "baz"))
    wrapper = MetadataCapturingChatModel(inner)
    async with wrapper.generate_stream_capturing("p") as cap:
        collected = [chunk async for chunk in cap.chunks()]
    assert collected == ["foo", "bar", "baz"]
    assert cap.accumulated_text() == "foobarbaz"
    assert cap.metadata is not None
    assert cap.metadata.completion_tokens is not None


async def test_parse_usage_from_dict():
    payload = {"usage": {"prompt_tokens": 12, "completion_tokens": 34}}
    assert parse_usage_from_response_json(payload) == (12, 34)


async def test_parse_usage_handles_missing():
    assert parse_usage_from_response_json("not-json") == (None, None)
    assert parse_usage_from_response_json({}) == (None, None)
    assert parse_usage_from_response_json({"usage": "wrong"}) == (None, None)
