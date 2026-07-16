"""DuckDuckGo Instant Answer adapter — ``GET api.duckduckgo.com/?q=..``.

Key-free, but **limited by design**: this hits DuckDuckGo's *Instant
Answer* API only (``AbstractText`` / ``Answer`` / ``RelatedTopics``),
NOT full-web search. It shines for definitional / entity lookups
("what is X", "who is Y") and returns empty for most open-ended
queries. The admin UI and docs label it accordingly so operators reach
for SearXNG when they need real full-web coverage.

We deliberately do **not** scrape DuckDuckGo's HTML results page: that
is a crawler behaviour, violates their ToS, and breaks on layout /
rate-limit changes. Empty Instant-Answer results simply yield an empty
``SearchResponse`` (the tool then tells the LLM "no results") rather
than escalating to scraping.
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

_DEFAULT_BASE_URL = "https://api.duckduckgo.com"


class DuckDuckGoSearchClient(SearchClientPort):
    """Async wrapper over DuckDuckGo's Instant Answer JSON endpoint.

    No auth. ``base_url`` defaults to the public endpoint; overridable
    only for tests / proxies.
    """

    def __init__(
        self,
        *,
        base_url: str = _DEFAULT_BASE_URL,
        timeout_seconds: float = 15.0,
    ) -> None:
        self._base_url = (base_url or _DEFAULT_BASE_URL).rstrip("/")
        self._timeout = timeout_seconds

    async def search(
        self, *, query: str, max_results: int,
    ) -> SearchResponse:
        params = {
            "q": query,
            "format": "json",
            "no_html": "1",
            "no_redirect": "1",
            "skip_disambig": "1",
        }
        url = f"{self._base_url}/"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.get(url, params=params)
        except httpx.TimeoutException as exc:
            raise SearchError("搜尋逾時") from exc
        except httpx.HTTPError as exc:
            raise SearchError(f"搜尋連線失敗：{exc}") from exc

        if response.status_code >= 400:
            body_preview = response.text[:200] if response.text else ""
            _LOGGER.warning(
                "duckduckgo search failed: status=%s body=%s",
                response.status_code, body_preview,
            )
            raise SearchError(
                f"DuckDuckGo 回應錯誤（{response.status_code}）",
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise SearchError("DuckDuckGo 回應不是 JSON") from exc
        if not isinstance(data, Mapping):
            return SearchResponse(answer="", results=[])

        # Instant Answer synthesizes AbstractText / Answer; treat either
        # as the fused answer chunk (whichever is populated).
        answer_raw = str(
            data.get("AbstractText") or data.get("Answer") or "",
        )
        answer = truncate_answer(answer_raw)

        results: list[SearchResult] = []
        abstract_url = str(data.get("AbstractURL") or "").strip()
        if abstract_url:
            results.append(
                SearchResult(
                    title=str(data.get("Heading") or "").strip() or abstract_url,
                    url=abstract_url,
                    snippet=truncate_snippet(answer_raw),
                ),
            )
        for topic in _iter_related_topics(data.get("RelatedTopics")):
            if len(results) >= max_results:
                break
            link = str(topic.get("FirstURL") or "").strip()
            if not link:
                continue
            text = str(topic.get("Text") or "").strip()
            results.append(
                SearchResult(
                    title=text.split(" - ", 1)[0] if text else link,
                    url=link,
                    snippet=truncate_snippet(text),
                ),
            )

        return SearchResponse(answer=answer, results=results[:max_results])


def _iter_related_topics(raw: object):
    """Yield flat topic mappings, unwrapping DDG's nested ``Topics`` groups."""
    if not isinstance(raw, list):
        return
    for entry in raw:
        if not isinstance(entry, Mapping):
            continue
        nested = entry.get("Topics")
        if isinstance(nested, list):
            for sub in nested:
                if isinstance(sub, Mapping):
                    yield sub
        else:
            yield entry
