"""BDD for ``WebFetchTool`` + ``HttpxReadabilityFetcher``.

Tool-layer tests stub ``WebFetchPort`` directly. Fetcher-layer tests
use ``httpx.MockTransport`` to serve canned HTML so we exercise the
readability path end-to-end without the network.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from kokoro_link.contracts.tool import ToolContext
from kokoro_link.contracts.web_fetch import (
    WebFetchError,
    WebFetchResult,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.tools.webfetch.httpx_fetcher import (
    HttpxReadabilityFetcher,
)
from kokoro_link.infrastructure.tools.webfetch.tool import WebFetchTool


# ---- tool layer -------------------------------------------------------


class _StubFetcher:
    def __init__(
        self,
        *,
        result: WebFetchResult | None = None,
        raise_exc: Exception | None = None,
    ) -> None:
        self.result = result
        self.raise_exc = raise_exc
        self.calls: list[str] = []

    async def fetch(self, url: str) -> WebFetchResult:
        self.calls.append(url)
        if self.raise_exc is not None:
            raise self.raise_exc
        assert self.result is not None
        return self.result


def _character() -> Character:
    return Character.create(
        name="Yuki",
        summary="",
        personality=[], interests=[], speaking_style="soft",
        boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
        allowed_tools=["web_fetch"],
    )


def _ctx(args: dict[str, Any]) -> ToolContext:
    return ToolContext(character=_character(), arguments=args)


@pytest.mark.asyncio
async def test_happy_path_wraps_content_in_boundary() -> None:
    fetcher = _StubFetcher(result=WebFetchResult(
        url="https://zh.wikipedia.org/wiki/Foo",
        title="Foo — 維基百科",
        text="這是一段關於 Foo 的正文內容，長度足夠。",
    ))
    tool = WebFetchTool(fetcher=fetcher)

    result = await tool.invoke(_ctx({"url": "https://zh.wikipedia.org/wiki/Foo"}))

    assert result.ok is True
    assert "Foo — 維基百科" in result.output_text
    assert "https://zh.wikipedia.org/wiki/Foo" in result.output_text
    # Prompt-injection boundary renders around the body.
    assert "===== 以下為外部網頁內容" in result.output_text
    assert "===== 外部網頁內容結束 =====" in result.output_text
    assert fetcher.calls == ["https://zh.wikipedia.org/wiki/Foo"]


@pytest.mark.asyncio
async def test_truncated_flag_surfaced_to_llm() -> None:
    fetcher = _StubFetcher(result=WebFetchResult(
        url="https://e.com/x", title="T", text="abc", truncated=True,
    ))
    tool = WebFetchTool(fetcher=fetcher)

    result = await tool.invoke(_ctx({"url": "https://e.com/x"}))

    assert result.ok is True
    assert "內容過長" in result.output_text


@pytest.mark.asyncio
async def test_missing_url_is_validation_failure() -> None:
    fetcher = _StubFetcher(result=WebFetchResult(url="", title="", text=""))
    tool = WebFetchTool(fetcher=fetcher)

    result = await tool.invoke(_ctx({"url": "   "}))

    assert result.ok is False
    assert "url" in (result.error or "")
    assert fetcher.calls == []


@pytest.mark.asyncio
async def test_fetch_error_becomes_tool_failure() -> None:
    fetcher = _StubFetcher(raise_exc=WebFetchError("抓取逾時"))
    tool = WebFetchTool(fetcher=fetcher)

    result = await tool.invoke(_ctx({"url": "https://e.com/"}))

    assert result.ok is False
    assert result.error == "抓取逾時"


@pytest.mark.asyncio
async def test_unexpected_exception_is_contained() -> None:
    fetcher = _StubFetcher(raise_exc=RuntimeError("boom"))
    tool = WebFetchTool(fetcher=fetcher)

    result = await tool.invoke(_ctx({"url": "https://e.com/"}))

    assert result.ok is False
    assert "抓取失敗" in (result.error or "")


# ---- fetcher layer ----------------------------------------------------


def _with_mock_transport(transport: httpx.MockTransport) -> Any:
    """Monkey-patch ``httpx.AsyncClient`` to use the given transport.

    Exposing ``transport=`` as a ctor arg on the fetcher would leak
    test plumbing into the production signature. Monkeypatching inside
    a contextmanager keeps the seam local to the test.
    """
    original_init = httpx.AsyncClient.__init__

    def patched_init(self: httpx.AsyncClient, **kwargs: Any) -> None:
        kwargs["transport"] = transport
        original_init(self, **kwargs)

    class _Ctx:
        def __enter__(self) -> None:
            httpx.AsyncClient.__init__ = patched_init  # type: ignore[method-assign]

        def __exit__(self, *_: Any) -> None:
            httpx.AsyncClient.__init__ = original_init  # type: ignore[method-assign]

    return _Ctx()


_SAMPLE_HTML = """
<!doctype html>
<html><head><title>Yuki Wiki — 正文測試</title></head>
<body>
  <nav>首頁 登入 搜尋</nav>
  <article>
    <h1>Yuki Wiki — 正文測試</h1>
    <p>這是一段實際的正文內容，描述角色設定、妝容特徵與氛圍。</p>
    <p>第二段繼續講世界觀，提到具體的地點與時間。</p>
    <script>alert('nope')</script>
  </article>
  <footer>版權所有</footer>
</body></html>
"""


@pytest.mark.asyncio
async def test_fetcher_extracts_article_and_strips_scripts() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            content=_SAMPLE_HTML.encode("utf-8"),
        )

    fetcher = HttpxReadabilityFetcher()
    with _with_mock_transport(httpx.MockTransport(handler)):
        result = await fetcher.fetch("https://e.com/page")

    assert "正文測試" in result.title
    assert "角色設定" in result.text
    assert "alert('nope')" not in result.text
    assert result.truncated is False


@pytest.mark.asyncio
async def test_fetcher_rejects_non_http_url() -> None:
    fetcher = HttpxReadabilityFetcher()
    with pytest.raises(WebFetchError):
        await fetcher.fetch("ftp://e.com/")


@pytest.mark.asyncio
async def test_fetcher_rejects_non_html_content_type() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "application/pdf"}, content=b"%PDF-1.4",
        )

    fetcher = HttpxReadabilityFetcher()
    with _with_mock_transport(httpx.MockTransport(handler)):
        with pytest.raises(WebFetchError):
            await fetcher.fetch("https://e.com/paper.pdf")


@pytest.mark.asyncio
async def test_fetcher_maps_http_error_to_webfetch_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="not found")

    fetcher = HttpxReadabilityFetcher()
    with _with_mock_transport(httpx.MockTransport(handler)):
        with pytest.raises(WebFetchError):
            await fetcher.fetch("https://e.com/missing")


@pytest.mark.asyncio
async def test_fetcher_truncates_long_text() -> None:
    long_para = "角色設定與氛圍細節。" * 2000  # ~ 20k chars
    html = f"<html><body><article><p>{long_para}</p></article></body></html>"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            content=html.encode("utf-8"),
        )

    fetcher = HttpxReadabilityFetcher(max_text_chars=500)
    with _with_mock_transport(httpx.MockTransport(handler)):
        result = await fetcher.fetch("https://e.com/long")

    assert result.truncated is True
    assert result.text.endswith("…")
    assert len(result.text) <= 501
