"""BDD for ``DuckDuckGoSearchClient`` (Instant Answer only).

Covers AbstractText → answer, RelatedTopics (incl. nested Topics
groups) → results, empty Instant Answer → empty response (no crash, no
scraping fallback), and error mapping.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from kokoro_link.infrastructure.tools.websearch import (
    DuckDuckGoSearchClient,
    SearchError,
)


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


@pytest.mark.asyncio
async def test_abstract_and_related_topics_parsed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params.get("format") == "json"
        return httpx.Response(
            200,
            json={
                "Heading": "Python",
                "AbstractText": "Python is a programming language.",
                "AbstractURL": "https://en.wikipedia.org/wiki/Python",
                "RelatedTopics": [
                    {"FirstURL": "https://e.com/a", "Text": "Alpha - the first"},
                    {
                        "Name": "group",
                        "Topics": [
                            {"FirstURL": "https://e.com/b", "Text": "Beta - nested"},
                        ],
                    },
                    {"Text": "no url dropped"},
                ],
            },
        )

    transport = httpx.MockTransport(handler)
    client = DuckDuckGoSearchClient()
    response = await _run_with_transport(
        lambda: client.search(query="python", max_results=10), transport,
    )

    assert "Python is a programming language" in response.answer
    urls = [r.url for r in response.results]
    assert "https://en.wikipedia.org/wiki/Python" in urls
    assert "https://e.com/a" in urls
    assert "https://e.com/b" in urls  # nested Topics unwrapped


@pytest.mark.asyncio
async def test_empty_instant_answer_is_empty_response_not_crash() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"AbstractText": "", "AbstractURL": "", "RelatedTopics": []},
        )

    transport = httpx.MockTransport(handler)
    client = DuckDuckGoSearchClient()
    response = await _run_with_transport(
        lambda: client.search(query="obscure", max_results=5), transport,
    )
    assert response.answer == ""
    assert response.results == []


@pytest.mark.asyncio
async def test_max_results_caps_related_topics() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "AbstractText": "",
                "AbstractURL": "",
                "RelatedTopics": [
                    {"FirstURL": f"https://e.com/{i}", "Text": f"t{i}"}
                    for i in range(10)
                ],
            },
        )

    transport = httpx.MockTransport(handler)
    client = DuckDuckGoSearchClient()
    response = await _run_with_transport(
        lambda: client.search(query="q", max_results=3), transport,
    )
    assert len(response.results) == 3


@pytest.mark.asyncio
async def test_http_error_maps_to_search_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="down")

    transport = httpx.MockTransport(handler)
    client = DuckDuckGoSearchClient()
    with pytest.raises(SearchError):
        await _run_with_transport(
            lambda: client.search(query="q", max_results=5), transport,
        )
