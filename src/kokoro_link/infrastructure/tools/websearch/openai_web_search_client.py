"""OpenAI Responses web-search adapter — ``POST {base_url}/responses``.

The most "hands-off" of the :class:`SearchClientPort` backends: instead
of returning raw snippets for the LLM to digest next turn, this hands the
query to OpenAI's Responses API with the built-in ``web_search`` tool.
OpenAI's model does the searching, reading, and synthesis server-side and
returns one fused answer plus ``url_citation`` annotations — exactly the
shape :class:`SearchResponse` already carries (``answer`` + ``results``),
which :class:`WebSearchTool` renders answer-first with no changes.

Design notes:

* The synthesised text maps to :attr:`SearchResponse.answer` (the money
  shot — ``WebSearchTool`` prints it first as "摘要："), and each
  ``url_citation`` annotation maps to one :class:`SearchResult`. The
  snippet is the cited substring of the answer (via ``start_index`` /
  ``end_index``) when present, else empty.
* This is a distinct endpoint from chat completions (``/v1/responses``,
  not ``/v1/chat/completions``), so it deliberately does NOT reuse the
  ``OpenAICompatibleChatModel`` — it is its own thin ``SearchClientPort``
  over ``httpx``, same as the Tavily / SearXNG / DuckDuckGo adapters.
* The model does live searching + synthesis, so the default timeout is
  longer (30 s) than the REST-style search APIs (15 s).
* ``tool_type`` lets the operator reconcile the built-in tool name with
  their chosen model (GA ``web_search`` vs older ``web_search_preview``).
  A model that doesn't support the requested tool returns a 4xx, which we
  map to a readable ``SearchError`` so the operator can adjust.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

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

_DEFAULT_BASE_URL = "https://api.openai.com/v1"


class OpenAIWebSearchClient(SearchClientPort):
    """Async wrapper over OpenAI's Responses API built-in web search.

    ``api_key`` and ``model`` are required. ``tool_type`` defaults to the
    GA ``web_search``; ``search_context_size`` (``low`` / ``medium`` /
    ``high``) is the cost/latency knob and is only sent when set.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str = _DEFAULT_BASE_URL,
        tool_type: str = "web_search",
        search_context_size: str | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        if not api_key:
            raise ValueError("OpenAIWebSearchClient requires an api_key")
        if not model:
            raise ValueError("OpenAIWebSearchClient requires a model")
        self._api_key = api_key
        self._model = model
        self._base_url = (base_url or _DEFAULT_BASE_URL).rstrip("/")
        self._tool_type = tool_type or "web_search"
        self._search_context_size = search_context_size or None
        self._timeout = timeout_seconds

    async def search(
        self, *, query: str, max_results: int,
    ) -> SearchResponse:
        tool: dict[str, Any] = {"type": self._tool_type}
        if self._search_context_size:
            tool["search_context_size"] = self._search_context_size
        payload = {
            "model": self._model,
            "input": query,
            "tools": [tool],
        }
        url = f"{self._base_url}/responses"
        headers = {"Authorization": f"Bearer {self._api_key}"}
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(url, json=payload, headers=headers)
        except httpx.TimeoutException as exc:
            raise SearchError("搜尋逾時") from exc
        except httpx.HTTPError as exc:
            raise SearchError(f"搜尋連線失敗：{exc}") from exc

        if response.status_code >= 400:
            message = _extract_error_message(response)
            _LOGGER.warning(
                "openai web search failed: status=%s body=%s",
                response.status_code,
                response.text[:200] if response.text else "",
            )
            # A 4xx here is often "this model can't use the requested
            # web_search tool type" — surface the upstream message so the
            # operator can switch the model or tool type in Admin.
            suffix = f"：{message}" if message else ""
            raise SearchError(
                f"OpenAI 搜尋 API 回應錯誤（{response.status_code}）{suffix}",
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise SearchError("OpenAI 搜尋 API 回應不是 JSON") from exc
        if not isinstance(data, Mapping):
            return SearchResponse(answer="", results=[])

        answer_parts: list[str] = []
        results: list[SearchResult] = []
        seen_urls: set[str] = set()
        for message in _iter_output_messages(data.get("output")):
            for text, annotations in _iter_output_text(message.get("content")):
                if text:
                    answer_parts.append(text)
                for citation in _iter_url_citations(annotations, text):
                    if len(results) >= max_results:
                        break
                    if citation.url in seen_urls:
                        continue
                    seen_urls.add(citation.url)
                    results.append(citation)
            if len(results) >= max_results:
                break

        answer = truncate_answer("\n".join(p for p in answer_parts if p))
        return SearchResponse(answer=answer, results=results[:max_results])


def _extract_error_message(response: httpx.Response) -> str:
    """Pull ``error.message`` from an OpenAI error body, best-effort."""
    try:
        data = response.json()
    except ValueError:
        return ""
    if not isinstance(data, Mapping):
        return ""
    error = data.get("error")
    if isinstance(error, Mapping):
        return str(error.get("message") or "").strip()
    return ""


def _iter_output_messages(raw: object):
    """Yield the assistant ``message`` items from the ``output`` array.

    The Responses ``output`` list interleaves tool-call items (e.g.
    ``web_search_call``) with ``message`` items; only the latter carry the
    synthesised text and citations."""
    if not isinstance(raw, list):
        return
    for item in raw:
        if isinstance(item, Mapping) and item.get("type") == "message":
            yield item


def _iter_output_text(raw: object):
    """Yield ``(text, annotations)`` for each ``output_text`` content part."""
    if not isinstance(raw, list):
        return
    for part in raw:
        if not isinstance(part, Mapping):
            continue
        if part.get("type") != "output_text":
            continue
        text = str(part.get("text") or "")
        annotations = part.get("annotations")
        yield text, annotations if isinstance(annotations, list) else []


def _iter_url_citations(annotations: list, text: str):
    """Yield :class:`SearchResult` from ``url_citation`` annotations.

    The snippet is the cited slice of ``text`` (``start_index`` /
    ``end_index``) when the indices are valid, else empty — the fused
    ``answer`` already carries the substance, so a missing slice is fine."""
    for annotation in annotations:
        if not isinstance(annotation, Mapping):
            continue
        if annotation.get("type") != "url_citation":
            continue
        link = str(annotation.get("url") or "").strip()
        if not link:
            continue
        title = str(annotation.get("title") or "").strip() or link
        yield SearchResult(
            title=title,
            url=link,
            snippet=truncate_snippet(_cited_slice(text, annotation)),
        )


def _cited_slice(text: str, annotation: Mapping[str, Any]) -> str:
    start = annotation.get("start_index")
    end = annotation.get("end_index")
    if (
        isinstance(start, int)
        and isinstance(end, int)
        and 0 <= start < end <= len(text)
    ):
        return text[start:end]
    return ""
