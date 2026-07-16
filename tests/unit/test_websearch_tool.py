"""BDD for ``TavilyWebSearchTool``.

Stubs the HTTP client so we don't hit the network. Covers:

- happy path: query → results → ``output_text`` lists them with URLs
- empty results surface a friendly "no results" message (success, not
  failure — so the LLM can tell the user gracefully)
- missing query → validation failure
- client raises ``TavilyError`` → ``ToolResult.failure`` with the message
- unexpected exception is contained (adapter isolation)
- ``max_results`` is clamped and respected when present
- long snippets are truncated so the tool result can't blow the prompt
"""

from __future__ import annotations

from typing import Any

import pytest

from kokoro_link.contracts.tool import ToolContext
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.value_objects.character_state import CharacterState
import httpx

from kokoro_link.infrastructure.tools.websearch.tool import (
    TavilyClient,
    TavilyError,
    TavilySearchResponse,
    TavilySearchResult,
    TavilyWebSearchTool,
)


class _StubClient:
    def __init__(
        self,
        *,
        results: list[TavilySearchResult] | None = None,
        answer: str = "",
        raise_exc: Exception | None = None,
    ) -> None:
        self.results = results or []
        self.answer = answer
        self.raise_exc = raise_exc
        self.calls: list[tuple[str, int]] = []

    async def search(
        self, *, query: str, max_results: int,
    ) -> TavilySearchResponse:
        self.calls.append((query, max_results))
        if self.raise_exc is not None:
            raise self.raise_exc
        return TavilySearchResponse(answer=self.answer, results=self.results)


def _character() -> Character:
    return Character.create(
        name="Yuki",
        summary="",
        personality=[], interests=[], speaking_style="soft",
        boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
        allowed_tools=["web_search"],
    )


def _ctx(args: dict[str, Any]) -> ToolContext:
    return ToolContext(character=_character(), arguments=args)


@pytest.mark.asyncio
async def test_happy_path_returns_formatted_snippets() -> None:
    client = _StubClient(
        answer="Claude Opus 4.7 於 2026 年發布，改善編碼與長上下文表現。",
        results=[
            TavilySearchResult(
                title="Claude Opus 4.7 announcement",
                url="https://example.com/a",
                snippet="Anthropic released Claude Opus 4.7 in 2026.",
            ),
            TavilySearchResult(
                title="Model card",
                url="https://example.com/b",
                snippet="Opus 4.7 improves coding and long-context reasoning.",
            ),
        ],
    )
    tool = TavilyWebSearchTool(client=client)

    result = await tool.invoke(_ctx({"query": "Claude Opus 4.7"}))

    assert result.ok is True
    # Synthesized answer renders first (most useful chunk).
    assert "摘要：Claude Opus 4.7 於 2026" in result.output_text
    assert "https://example.com/a" in result.output_text
    assert "https://example.com/b" in result.output_text
    assert result.attachments == ()
    assert client.calls == [("Claude Opus 4.7", 5)]


@pytest.mark.asyncio
async def test_empty_results_is_success_with_hint() -> None:
    client = _StubClient(results=[])
    tool = TavilyWebSearchTool(client=client)

    result = await tool.invoke(_ctx({"query": "nonsense"}))

    assert result.ok is True
    assert "沒有結果" in result.output_text


@pytest.mark.asyncio
async def test_missing_query_is_validation_error() -> None:
    client = _StubClient()
    tool = TavilyWebSearchTool(client=client)

    result = await tool.invoke(_ctx({"query": "   "}))

    assert result.ok is False
    assert "query" in (result.error or "")
    assert client.calls == []


@pytest.mark.asyncio
async def test_tavily_error_becomes_tool_failure() -> None:
    client = _StubClient(raise_exc=TavilyError("搜尋逾時"))
    tool = TavilyWebSearchTool(client=client)

    result = await tool.invoke(_ctx({"query": "foo"}))

    assert result.ok is False
    assert result.error == "搜尋逾時"


@pytest.mark.asyncio
async def test_unexpected_exception_is_contained() -> None:
    client = _StubClient(raise_exc=RuntimeError("boom"))
    tool = TavilyWebSearchTool(client=client)

    result = await tool.invoke(_ctx({"query": "foo"}))

    assert result.ok is False
    assert "搜尋失敗" in (result.error or "")


@pytest.mark.asyncio
async def test_max_results_respected_and_clamped() -> None:
    client = _StubClient(results=[])
    tool = TavilyWebSearchTool(client=client, default_max_results=5)

    await tool.invoke(_ctx({"query": "a", "max_results": 3}))
    await tool.invoke(_ctx({"query": "b", "max_results": 999}))
    await tool.invoke(_ctx({"query": "c", "max_results": 0}))
    await tool.invoke(_ctx({"query": "d", "max_results": "bad"}))

    assert [n for _, n in client.calls] == [3, 10, 1, 5]


@pytest.mark.asyncio
async def test_tavily_client_parses_results_and_truncates_snippet() -> None:
    long_content = "段落 " * 400

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/search"
        return httpx.Response(
            200,
            json={
                "answer": "這是 Tavily 自己合成的摘要",
                "results": [
                    {
                        "title": "Foo",
                        "url": "https://e.com/foo",
                        "content": long_content,
                    },
                    {
                        "title": "NoUrl",
                        "url": "",
                        "content": "should be dropped",
                    },
                ],
            },
        )

    transport = httpx.MockTransport(handler)

    client = TavilyClient(api_key="test")
    # Swap in the mock transport by monkey-patching the AsyncClient
    # constructor. Simpler than exposing transport as ctor arg.
    original_init = httpx.AsyncClient.__init__

    def patched_init(self: httpx.AsyncClient, **kwargs: Any) -> None:
        kwargs["transport"] = transport
        original_init(self, **kwargs)

    httpx.AsyncClient.__init__ = patched_init  # type: ignore[method-assign]
    try:
        response = await client.search(query="q", max_results=3)
    finally:
        httpx.AsyncClient.__init__ = original_init  # type: ignore[method-assign]

    assert response.answer == "這是 Tavily 自己合成的摘要"
    assert len(response.results) == 1
    assert response.results[0].title == "Foo"
    assert response.results[0].url == "https://e.com/foo"
    assert response.results[0].snippet.endswith("…")
    assert len(response.results[0].snippet) <= 600


@pytest.mark.asyncio
async def test_tavily_client_maps_http_error_to_tavily_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="upstream down")

    transport = httpx.MockTransport(handler)
    client = TavilyClient(api_key="test")
    original_init = httpx.AsyncClient.__init__

    def patched_init(self: httpx.AsyncClient, **kwargs: Any) -> None:
        kwargs["transport"] = transport
        original_init(self, **kwargs)

    httpx.AsyncClient.__init__ = patched_init  # type: ignore[method-assign]
    try:
        with pytest.raises(TavilyError):
            await client.search(query="q", max_results=3)
    finally:
        httpx.AsyncClient.__init__ = original_init  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_long_snippet_truncated_in_client_layer() -> None:
    # Snippet truncation happens inside TavilyClient.search, but the
    # tool's job is to not explode when fed something borderline — use
    # the already-truncated VO here and verify output still formats.
    long = "x" * 200
    client = _StubClient(results=[
        TavilySearchResult(title="T", url="https://e.com", snippet=long),
    ])
    tool = TavilyWebSearchTool(client=client)

    result = await tool.invoke(_ctx({"query": "q"}))

    assert result.ok is True
    assert long in result.output_text
    assert "https://e.com" in result.output_text
