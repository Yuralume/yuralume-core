"""SearXNG search adapter — ``GET {base_url}/search?q=..&format=json``.

Key-free, full-web search backed by a self-hosted SearXNG instance
(the reported use case). SearXNG aggregates many upstream engines and
returns a ``results`` list; it has no single fused ``answer`` field, so
:attr:`SearchResponse.answer` is always empty and we render sources
only.

Operator gotcha (surfaced in the admin field hint + docs): SearXNG must
have ``json`` enabled under ``search.formats`` in its ``settings.yml``.
When it isn't, the instance answers the ``format=json`` request with an
HTML page (or ``403``) rather than JSON — we map that to a readable
error so the operator doesn't mistake it for a bug in this product.
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
    truncate_snippet,
)

_LOGGER = logging.getLogger(__name__)

# Browser-like request headers so SearXNG's limiter/botdetection passes on
# hardened/public instances. Its header methods each flag a non-browser
# request: http_user_agent blocks python-httpx/curl UAs; http_accept blocks
# any Accept without ``text/html``; http_accept_language blocks a missing
# Accept-Language; http_accept_encoding blocks an Accept-Encoding lacking
# gzip/deflate. We still get JSON back because ``format=json`` is a query
# param (SearXNG picks the response format from the query, not the Accept
# header), so a browser Accept here does not change the response shape. We
# advertise only gzip/deflate (both natively decoded by httpx) to avoid
# claiming a br/zstd encoding we might not be able to decompress.
_BROWSER_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
}


class SearXNGSearchClient(SearchClientPort):
    """Async wrapper over a SearXNG instance's JSON search endpoint.

    ``base_url`` is required (there is no public default SearXNG). An
    optional ``api_key`` is sent as a Bearer header for instances that
    sit behind an auth proxy; most self-hosted instances need none.
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None = None,
        language: str = "",
        timeout_seconds: float = 15.0,
    ) -> None:
        if not base_url:
            raise ValueError("SearXNGSearchClient requires a base_url")
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key or None
        self._language = language
        self._timeout = timeout_seconds

    async def search(
        self, *, query: str, max_results: int,
    ) -> SearchResponse:
        params: dict[str, str] = {"q": query, "format": "json"}
        if self._language:
            params["language"] = self._language
        headers: dict[str, str] = dict(_BROWSER_HEADERS)
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        url = f"{self._base_url}/search"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.get(url, params=params, headers=headers)
        except httpx.TimeoutException as exc:
            raise SearchError("搜尋逾時") from exc
        except httpx.HTTPError as exc:
            raise SearchError(f"搜尋連線失敗：{exc}") from exc

        if response.status_code >= 400:
            body_preview = response.text[:200] if response.text else ""
            _LOGGER.warning(
                "searxng search failed: status=%s body=%s",
                response.status_code, body_preview,
            )
            if response.status_code == 403:
                # 403 has two common causes on a JSON API call; we already
                # send browser-like headers to clear the bot-detection one,
                # so name BOTH rather than asserting the json-format cause:
                #   (1) the instance's limiter / bot-detection blocked the
                #       request (allowlist the caller IP or relax the
                #       limiter), or
                #   (2) json is not enabled under search.formats.
                raise SearchError(
                    "SearXNG 回應 403；可能原因："
                    "(1) 實例的 limiter / bot-detection 阻擋了此請求"
                    "（請將來源 IP 加入白名單或放寬 limiter），或 "
                    "(2) 尚未在 settings.yml 的 search.formats 開啟 json 格式",
                )
            raise SearchError(
                f"SearXNG 回應錯誤（{response.status_code}）"
                "；請確認實例已在 settings.yml 的 search.formats 開啟 json 格式",
            )

        content_type = response.headers.get("content-type", "")
        try:
            data = response.json()
        except ValueError as exc:
            # json format disabled → SearXNG serves the HTML results page.
            _LOGGER.warning(
                "searxng returned non-JSON (content-type=%s)", content_type,
            )
            raise SearchError(
                "SearXNG 回應不是 JSON"
                "；請在實例 settings.yml 的 search.formats 加入 json 後重試",
            ) from exc

        raw_results = data.get("results") if isinstance(data, Mapping) else None
        parsed: list[SearchResult] = []
        for item in raw_results or []:
            if not isinstance(item, Mapping):
                continue
            link = str(item.get("url") or "").strip()
            if not link:
                continue
            title = str(item.get("title") or "").strip()
            snippet = str(item.get("content") or "").strip()
            parsed.append(
                SearchResult(
                    title=title,
                    url=link,
                    snippet=truncate_snippet(snippet),
                ),
            )
            if len(parsed) >= max_results:
                break

        # SearXNG has no synthesized answer field → sources only.
        return SearchResponse(answer="", results=parsed)
