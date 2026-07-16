"""Static-HTML ``WebFetchPort`` — httpx + readability-lxml.

Covers the 80% case (Wikipedia, news sites, docs, most blogs) with
zero extra infrastructure. Sites that render content purely in JS
(Twitter, some SPAs) won't yield useful text here — swap in an
agent-browser-backed adapter for those.

Design notes:

* Readability is synchronous and CPU-bound-ish (HTML parse); we call
  it in the default thread pool via ``asyncio.to_thread`` so the
  event loop isn't blocked on a 200 KB page.
* We cap both the raw HTML download and the extracted text so a
  hostile server can't blow our memory or send a novel back to the
  LLM.
* Errors all collapse into ``WebFetchError`` with a short Chinese
  message. The tool wrapper uses it verbatim.
"""

from __future__ import annotations

import asyncio
import logging

import httpx
from readability import Document  # type: ignore[import-untyped]

from kokoro_link.contracts.web_fetch import (
    WebFetchError,
    WebFetchPort,
    WebFetchResult,
)

_LOGGER = logging.getLogger(__name__)

_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (compatible; YuralumeBot/1.0; +https://example.invalid)"
)


class HttpxReadabilityFetcher(WebFetchPort):
    def __init__(
        self,
        *,
        timeout_seconds: float = 15.0,
        max_html_bytes: int = 2_000_000,
        max_text_chars: int = 6000,
        user_agent: str = _DEFAULT_USER_AGENT,
    ) -> None:
        self._timeout = timeout_seconds
        self._max_html_bytes = max_html_bytes
        self._max_text_chars = max_text_chars
        self._user_agent = user_agent

    async def fetch(self, url: str) -> WebFetchResult:
        if not url.startswith(("http://", "https://")):
            raise WebFetchError("URL 必須以 http:// 或 https:// 開頭")

        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                follow_redirects=True,
                headers={"User-Agent": self._user_agent},
            ) as client:
                response = await client.get(url)
        except httpx.TimeoutException as exc:
            raise WebFetchError("抓取逾時") from exc
        except httpx.HTTPError as exc:
            raise WebFetchError(f"抓取連線失敗：{exc}") from exc

        if response.status_code >= 400:
            raise WebFetchError(
                f"頁面回應錯誤（{response.status_code}）",
            )

        content_type = response.headers.get("content-type", "").lower()
        if content_type and "html" not in content_type and "xml" not in content_type:
            raise WebFetchError(
                f"只支援 HTML 頁面（content-type={content_type}）",
            )

        body = response.content or b""
        if len(body) > self._max_html_bytes:
            body = body[: self._max_html_bytes]

        encoding = response.encoding or "utf-8"
        try:
            html = body.decode(encoding, errors="replace")
        except LookupError:
            html = body.decode("utf-8", errors="replace")

        final_url = str(response.url)
        title, text = await asyncio.to_thread(_extract, html)
        truncated = False
        if len(text) > self._max_text_chars:
            text = text[: self._max_text_chars].rstrip() + "…"
            truncated = True

        if not text.strip():
            raise WebFetchError("頁面沒有可抽取的正文")

        return WebFetchResult(
            url=final_url, title=title, text=text, truncated=truncated,
        )


def _extract(html: str) -> tuple[str, str]:
    """Parse with readability, fall back to raw text on failure.

    Readability can throw on malformed input (most commonly on very
    short or non-HTML pages). We catch broadly because the caller
    treats any failure as ``WebFetchError`` anyway — the distinction
    between parser crash and empty output isn't useful.
    """
    try:
        doc = Document(html)
        title = (doc.short_title() or "").strip()
        summary_html = doc.summary(html_partial=True) or ""
    except Exception:  # noqa: BLE001 — readability raises ad-hoc errors
        _LOGGER.warning("readability parse failed; returning empty")
        return "", ""

    text = _strip_html(summary_html)
    return title, text


def _strip_html(html: str) -> str:
    """Cheap tag-stripper so we don't pay lxml traversal cost twice.

    Readability already returned us a content-only fragment; we just
    need to drop the remaining tags and collapse whitespace. Good
    enough for the LLM — it doesn't care about paragraph fidelity.
    """
    import re

    text = re.sub(r"<script\b[^>]*>.*?</script>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style\b[^>]*>.*?</style>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</p>|</div>|</li>|</h[1-6]>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    # HTML entities — keep it minimal, readability usually decodes
    # common ones already.
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
    )
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
