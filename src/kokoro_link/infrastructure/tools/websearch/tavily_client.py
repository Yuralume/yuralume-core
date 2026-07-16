"""Tavily search adapter — ``POST https://api.tavily.com/search``.

One of the concrete :class:`SearchClientPort` implementations behind the
``web_search`` tool. Tavily is a paid, LLM-friendly search API that
returns snippet content plus its own fused ``answer`` summary.
"""

from __future__ import annotations

import logging
from typing import Mapping

import httpx

from kokoro_link.infrastructure.tools.websearch.tool import (
    SearchClientPort,
    SearchError,
    SearchResponse,
    SearchResult,
    truncate_answer,
    truncate_snippet,
)

_LOGGER = logging.getLogger(__name__)


class TavilyClient(SearchClientPort):
    """Thin async wrapper over Tavily's ``POST /search``.

    Stays stateless per call — a fresh ``httpx.AsyncClient`` per
    invocation is fine at search frequencies (one call per chat turn
    at most). Tavily has no streaming protocol, so we don't need a
    long-lived session.
    """

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.tavily.com",
        search_depth: str = "basic",
        timeout_seconds: float = 15.0,
    ) -> None:
        if not api_key:
            raise ValueError("TavilyClient requires an api_key")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._search_depth = search_depth
        self._timeout = timeout_seconds

    async def search(
        self, *, query: str, max_results: int,
    ) -> SearchResponse:
        payload = {
            "api_key": self._api_key,
            "query": query,
            "search_depth": self._search_depth,
            "max_results": max_results,
            # Tavily's own summary across all hits — cheap and usually
            # the money shot. The LLM reads this first.
            "include_answer": True,
            # Raw page content would blow the context budget; snippet
            # length alone (bumped in ``advanced`` depth) is enough.
            "include_raw_content": False,
        }
        url = f"{self._base_url}/search"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(url, json=payload)
        except httpx.TimeoutException as exc:
            raise SearchError("搜尋逾時") from exc
        except httpx.HTTPError as exc:
            raise SearchError(f"搜尋連線失敗：{exc}") from exc

        if response.status_code >= 400:
            # Tavily returns JSON errors, but body may be plain text on
            # infra failures — surface both shapes in the log, keep the
            # user-facing message short.
            body_preview = response.text[:200] if response.text else ""
            _LOGGER.warning(
                "tavily search failed: status=%s body=%s",
                response.status_code, body_preview,
            )
            raise SearchError(
                f"搜尋 API 回應錯誤（{response.status_code}）",
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise SearchError("搜尋 API 回應不是 JSON") from exc

        raw_results = data.get("results") or []
        parsed: list[SearchResult] = []
        for item in raw_results:
            if not isinstance(item, Mapping):
                continue
            title = str(item.get("title") or "").strip()
            link = str(item.get("url") or "").strip()
            snippet = str(item.get("content") or "").strip()
            if not link:
                continue
            parsed.append(
                SearchResult(
                    title=title,
                    url=link,
                    snippet=truncate_snippet(snippet),
                ),
            )

        answer = truncate_answer(str(data.get("answer") or ""))
        return SearchResponse(answer=answer, results=parsed)
