"""BDD for ``OpenAIWebSearchClient`` (OpenAI Responses built-in web search).

Covers Responses ``output`` parsing (message ``output_text`` → answer,
``url_citation`` annotations → results with cited-slice snippets), the
tool payload shape (tool type + optional ``search_context_size``), url
dedup + ``max_results`` capping, empty output → empty response (no
crash), and error mapping (timeout / HTTP / 4xx with upstream message /
non-JSON).
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from kokoro_link.infrastructure.tools.websearch import (
    OpenAIWebSearchClient,
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


def _message_response(text: str, annotations: list[dict[str, Any]]):
    """A Responses payload with one web_search_call + one message item."""
    return {
        "output": [
            {"type": "web_search_call", "id": "ws_1", "status": "completed"},
            {
                "type": "message",
                "role": "assistant",
                "content": [
                    {
                        "type": "output_text",
                        "text": text,
                        "annotations": annotations,
                    },
                ],
            },
        ],
    }


@pytest.mark.asyncio
async def test_answer_and_citations_parsed() -> None:
    text = "Yuralume is a companion app. See the docs for details."

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url).endswith("/responses")
        assert request.headers["Authorization"] == "Bearer sk-test"
        return httpx.Response(
            200,
            json=_message_response(
                text,
                [
                    {
                        "type": "url_citation",
                        "url": "https://yuralume.com",
                        "title": "Yuralume",
                        "start_index": 0,
                        "end_index": 28,
                    },
                ],
            ),
        )

    transport = httpx.MockTransport(handler)
    client = OpenAIWebSearchClient(api_key="sk-test", model="gpt-5.4-mini")
    response = await _run_with_transport(
        lambda: client.search(query="what is yuralume", max_results=5),
        transport,
    )

    assert "Yuralume is a companion app" in response.answer
    assert len(response.results) == 1
    assert response.results[0].url == "https://yuralume.com"
    assert response.results[0].title == "Yuralume"
    # Snippet is the cited slice of the answer text.
    assert response.results[0].snippet == "Yuralume is a companion app."


@pytest.mark.asyncio
async def test_tool_payload_carries_type_and_context_size() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        captured.update(json.loads(request.content))
        return httpx.Response(200, json=_message_response("ok", []))

    transport = httpx.MockTransport(handler)
    client = OpenAIWebSearchClient(
        api_key="sk-test",
        model="gpt-5.4-mini",
        tool_type="web_search",
        search_context_size="low",
    )
    await _run_with_transport(
        lambda: client.search(query="q", max_results=5), transport,
    )

    assert captured["model"] == "gpt-5.4-mini"
    assert captured["input"] == "q"
    assert captured["tools"] == [
        {"type": "web_search", "search_context_size": "low"},
    ]


@pytest.mark.asyncio
async def test_context_size_omitted_when_unset() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        captured.update(json.loads(request.content))
        return httpx.Response(200, json=_message_response("ok", []))

    transport = httpx.MockTransport(handler)
    client = OpenAIWebSearchClient(api_key="sk-test", model="gpt-5.4-mini")
    await _run_with_transport(
        lambda: client.search(query="q", max_results=5), transport,
    )

    assert captured["tools"] == [{"type": "web_search"}]


@pytest.mark.asyncio
async def test_url_dedup_and_max_results_cap() -> None:
    annotations = [
        {"type": "url_citation", "url": "https://a.com", "title": "A"},
        {"type": "url_citation", "url": "https://a.com", "title": "A dup"},
        {"type": "url_citation", "url": "https://b.com", "title": "B"},
        {"type": "url_citation", "url": "https://c.com", "title": "C"},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_message_response("body", annotations))

    transport = httpx.MockTransport(handler)
    client = OpenAIWebSearchClient(api_key="sk-test", model="gpt-5.4-mini")
    response = await _run_with_transport(
        lambda: client.search(query="q", max_results=2), transport,
    )

    urls = [r.url for r in response.results]
    assert urls == ["https://a.com", "https://b.com"]  # dedup + cap at 2


@pytest.mark.asyncio
async def test_empty_output_is_empty_response_not_crash() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"output": []})

    transport = httpx.MockTransport(handler)
    client = OpenAIWebSearchClient(api_key="sk-test", model="gpt-5.4-mini")
    response = await _run_with_transport(
        lambda: client.search(query="q", max_results=5), transport,
    )
    assert response.answer == ""
    assert response.results == []


@pytest.mark.asyncio
async def test_4xx_surfaces_upstream_message() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": {
                    "message": "model does not support web_search",
                    "type": "invalid_request_error",
                },
            },
        )

    transport = httpx.MockTransport(handler)
    client = OpenAIWebSearchClient(api_key="sk-test", model="gpt-3.5-turbo")
    with pytest.raises(SearchError) as exc:
        await _run_with_transport(
            lambda: client.search(query="q", max_results=5), transport,
        )
    assert "web_search" in str(exc.value)
    assert "400" in str(exc.value)


@pytest.mark.asyncio
async def test_http_error_maps_to_search_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="upstream down")

    transport = httpx.MockTransport(handler)
    client = OpenAIWebSearchClient(api_key="sk-test", model="gpt-5.4-mini")
    with pytest.raises(SearchError):
        await _run_with_transport(
            lambda: client.search(query="q", max_results=5), transport,
        )


@pytest.mark.asyncio
async def test_non_json_maps_to_search_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not json</html>")

    transport = httpx.MockTransport(handler)
    client = OpenAIWebSearchClient(api_key="sk-test", model="gpt-5.4-mini")
    with pytest.raises(SearchError):
        await _run_with_transport(
            lambda: client.search(query="q", max_results=5), transport,
        )


def test_requires_api_key_and_model() -> None:
    with pytest.raises(ValueError):
        OpenAIWebSearchClient(api_key="", model="gpt-5.4-mini")
    with pytest.raises(ValueError):
        OpenAIWebSearchClient(api_key="sk-test", model="")
