"""BDD for ``SearXNGSearchClient``.

Mirrors the Tavily client test pattern: MockTransport-backed httpx so
no network. Covers response parsing, max_results clamping, snippet
truncation, and the json-format-disabled → readable-error mapping (the
operator gotcha).
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from kokoro_link.infrastructure.tools.websearch import (
    SearchError,
    SearXNGSearchClient,
)


def _client_with(transport: httpx.MockTransport, **kwargs: Any) -> SearXNGSearchClient:
    client = SearXNGSearchClient(base_url="https://searxng.example.test", **kwargs)
    return client


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


def test_requires_base_url() -> None:
    with pytest.raises(ValueError):
        SearXNGSearchClient(base_url="")


@pytest.mark.asyncio
async def test_parses_results_and_has_no_answer() -> None:
    long_content = "段落 " * 400

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/search"
        assert request.url.params.get("format") == "json"
        assert request.url.params.get("q") == "q"
        return httpx.Response(
            200,
            json={
                "results": [
                    {"title": "Foo", "url": "https://e.com/foo", "content": long_content},
                    {"title": "NoUrl", "url": "", "content": "dropped"},
                    {"title": "Bar", "url": "https://e.com/bar", "content": "bar"},
                ],
            },
        )

    transport = httpx.MockTransport(handler)
    client = _client_with(transport)
    response = await _run_with_transport(
        lambda: client.search(query="q", max_results=5), transport,
    )

    assert response.answer == ""  # SearXNG has no fused answer
    assert len(response.results) == 2  # empty-url row dropped
    assert response.results[0].url == "https://e.com/foo"
    assert response.results[0].snippet.endswith("…")
    assert len(response.results[0].snippet) <= 600


@pytest.mark.asyncio
async def test_max_results_caps_returned_rows() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    {"title": f"t{i}", "url": f"https://e.com/{i}", "content": "c"}
                    for i in range(10)
                ],
            },
        )

    transport = httpx.MockTransport(handler)
    client = _client_with(transport)
    response = await _run_with_transport(
        lambda: client.search(query="q", max_results=3), transport,
    )
    assert len(response.results) == 3


@pytest.mark.asyncio
async def test_non_json_response_maps_to_readable_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # json format disabled → SearXNG serves the HTML results page.
        return httpx.Response(200, text="<html>results</html>", headers={"content-type": "text/html"})

    transport = httpx.MockTransport(handler)
    client = _client_with(transport)
    with pytest.raises(SearchError) as excinfo:
        await _run_with_transport(
            lambda: client.search(query="q", max_results=5), transport,
        )
    assert "json" in str(excinfo.value)


@pytest.mark.asyncio
async def test_403_maps_to_readable_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="forbidden")

    transport = httpx.MockTransport(handler)
    client = _client_with(transport)
    with pytest.raises(SearchError) as excinfo:
        await _run_with_transport(
            lambda: client.search(query="q", max_results=5), transport,
        )
    message = str(excinfo.value)
    # 403 now names BOTH plausible causes: the limiter/bot-detection block
    # (now that we send browser headers) and the json-format one.
    assert "json" in message
    assert "limiter" in message or "bot-detection" in message


@pytest.mark.asyncio
async def test_sends_browser_like_headers_for_botdetection() -> None:
    """SearXNG's limiter/botdetection flags non-browser requests. The client
    must send a browser User-Agent + Accept(text/html) + Accept-Language +
    Accept-Encoding(gzip/deflate) so those header methods pass, while still
    getting JSON via the ``format=json`` query param."""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        for name in ("user-agent", "accept", "accept-language", "accept-encoding"):
            seen[name] = request.headers.get(name, "")
        return httpx.Response(200, json={"results": []})

    transport = httpx.MockTransport(handler)
    client = _client_with(transport)
    await _run_with_transport(
        lambda: client.search(query="q", max_results=5), transport,
    )
    assert "python-httpx" not in seen["user-agent"].lower()
    assert "mozilla" in seen["user-agent"].lower()
    assert "text/html" in seen["accept"]
    assert seen["accept-language"] != ""
    assert "gzip" in seen["accept-encoding"] or "deflate" in seen["accept-encoding"]


@pytest.mark.asyncio
async def test_optional_api_key_sent_as_bearer() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization", "")
        return httpx.Response(200, json={"results": []})

    transport = httpx.MockTransport(handler)
    client = SearXNGSearchClient(base_url="https://searxng.example.test", api_key="k")
    await _run_with_transport(
        lambda: client.search(query="q", max_results=5), transport,
    )
    assert seen["auth"] == "Bearer k"
