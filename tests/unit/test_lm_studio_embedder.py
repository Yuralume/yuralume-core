"""LMStudioEmbedder tests — mock the HTTP call, verify fail-loud behaviour."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from kokoro_link.contracts.embedder import EmbedderError
from kokoro_link.infrastructure.embedder.lm_studio import LMStudioEmbedder


def _mock_transport(handler) -> httpx.MockTransport:  # type: ignore[no-untyped-def]
    return httpx.MockTransport(handler)


class _RecordingClient:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, handler) -> None:  # noqa: ANN001
        self._monkeypatch = monkeypatch
        self._handler = handler
        self.calls: list[httpx.Request] = []

    def install(self) -> None:
        calls = self.calls
        handler = self._handler
        orig = httpx.AsyncClient

        def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
            def wrapped(request: httpx.Request) -> httpx.Response:
                calls.append(request)
                return handler(request)
            kwargs["transport"] = _mock_transport(wrapped)
            return orig(*args, **kwargs)

        self._monkeypatch.setattr(
            "kokoro_link.infrastructure.embedder.lm_studio.httpx.AsyncClient",
            factory,
        )


@pytest.mark.asyncio
async def test_embed_single_returns_vector(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": [{"index": 0, "embedding": [0.1, 0.2, 0.3]}]},
        )

    recorder = _RecordingClient(monkeypatch, handler)
    recorder.install()

    embedder = LMStudioEmbedder(base_url="http://fake/v1", model="bge-m3", dimension=3)
    vector = await embedder.embed("hello")
    assert vector == (0.1, 0.2, 0.3)
    assert len(recorder.calls) == 1
    assert str(recorder.calls[0].url).endswith("/embeddings")


@pytest.mark.asyncio
async def test_embed_many_preserves_order(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": 1, "embedding": [2.0]},
                    {"index": 0, "embedding": [1.0]},
                ]
            },
        )

    _RecordingClient(monkeypatch, handler).install()

    embedder = LMStudioEmbedder(base_url="http://fake/v1", model="bge-m3", dimension=1)
    vectors = await embedder.embed_many(["first", "second"])
    assert vectors == [(1.0,), (2.0,)]


@pytest.mark.asyncio
async def test_http_error_raises_embedder_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail-loud: HTTP failures must propagate, not silently yield None."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    _RecordingClient(monkeypatch, handler).install()

    embedder = LMStudioEmbedder(base_url="http://fake/v1", model="m", dimension=3)
    with pytest.raises(EmbedderError):
        await embedder.embed("oops")


@pytest.mark.asyncio
async def test_malformed_response_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": "not-a-list"})

    _RecordingClient(monkeypatch, handler).install()

    embedder = LMStudioEmbedder(base_url="http://fake/v1", model="m", dimension=3)
    with pytest.raises(EmbedderError):
        await embedder.embed_many(["a", "b"])


@pytest.mark.asyncio
async def test_partial_response_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Server returns vectors for some but not all inputs."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": [{"index": 0, "embedding": [1.0]}]},  # missing index 1
        )

    _RecordingClient(monkeypatch, handler).install()

    embedder = LMStudioEmbedder(base_url="http://fake/v1", model="m", dimension=1)
    with pytest.raises(EmbedderError):
        await embedder.embed_many(["a", "b"])


@pytest.mark.asyncio
async def test_authorization_header_set_when_key_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.headers.get("Authorization", ""))
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.0]}]})

    _RecordingClient(monkeypatch, handler).install()

    embedder = LMStudioEmbedder(
        base_url="http://fake/v1", model="m", api_key="secret-key", dimension=1,
    )
    await embedder.embed("hi")
    assert captured[0] == "Bearer secret-key"


@pytest.mark.asyncio
async def test_empty_input_short_circuits(monkeypatch: pytest.MonkeyPatch) -> None:
    called = {"value": False}

    def handler(request: httpx.Request) -> httpx.Response:
        called["value"] = True
        return httpx.Response(200, json={"data": []})

    _RecordingClient(monkeypatch, handler).install()

    embedder = LMStudioEmbedder(base_url="http://fake/v1", model="m", dimension=1)
    assert await embedder.embed_many([]) == []
    assert not called["value"]


def test_is_operational_true() -> None:
    embedder = LMStudioEmbedder(base_url="http://fake/v1", model="m", dimension=1)
    assert embedder.is_operational is True
