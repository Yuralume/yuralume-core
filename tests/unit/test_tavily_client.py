"""BDD for ``TavilyClient``.

MockTransport-backed httpx so no network. Focuses on the 2026-07-16 auth
change (Authorization: Bearer header, the SDK-documented method) while the
legacy body ``api_key`` is kept for backward compat, plus response parsing
and error mapping.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from kokoro_link.infrastructure.tools.websearch import SearchError, TavilyClient


def _client_with(transport: httpx.MockTransport, **kwargs: Any) -> TavilyClient:
    return TavilyClient(api_key="tvly-secret", **kwargs)


async def _run_with_transport(coro_factory, transport: httpx.MockTransport):
    original_init = httpx.AsyncClient.__init__

    def patched_init(self: httpx.AsyncClient, **kwargs: Any) -> None:
        kwargs["transport"] = transport
        original_init(self, **kwargs)

    httpx.AsyncClient.__init__ = patched_init  # type: ignore[method-assign]
    try:
        return await coro_factory()
    finally:
        httpx.AsyncClient.__init__ = original_init  # type: ignore[method-assign]


def test_requires_api_key() -> None:
    with pytest.raises(ValueError):
        TavilyClient(api_key="")


@pytest.mark.asyncio
async def test_sends_bearer_header_and_legacy_body_key() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization", "")
        import json

        seen["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"answer": "", "results": []})

    transport = httpx.MockTransport(handler)
    client = _client_with(transport)
    await _run_with_transport(
        lambda: client.search(query="q", max_results=5), transport,
    )
    # Header auth is the forward-compatible method (SDK + current reference).
    assert seen["auth"] == "Bearer tvly-secret"
    # Body key kept for older/self-hosted gateways.
    assert seen["body"]["api_key"] == "tvly-secret"


@pytest.mark.asyncio
async def test_parses_answer_and_results() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/search"
        return httpx.Response(
            200,
            json={
                "answer": "the fused answer",
                "results": [
                    {"title": "Foo", "url": "https://e.com/foo", "content": "foo body"},
                    {"title": "NoUrl", "url": "", "content": "dropped"},
                ],
            },
        )

    transport = httpx.MockTransport(handler)
    client = _client_with(transport)
    response = await _run_with_transport(
        lambda: client.search(query="q", max_results=5), transport,
    )
    assert response.answer == "the fused answer"
    assert len(response.results) == 1  # empty-url row dropped
    assert response.results[0].url == "https://e.com/foo"


@pytest.mark.asyncio
async def test_http_error_maps_to_readable_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"})

    transport = httpx.MockTransport(handler)
    client = _client_with(transport)
    with pytest.raises(SearchError):
        await _run_with_transport(
            lambda: client.search(query="q", max_results=5), transport,
        )
