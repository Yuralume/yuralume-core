"""Web-search tool package.

Ships the provider-neutral ``web_search`` ``ToolPort`` adapter
(:class:`WebSearchTool`) plus three concrete :class:`SearchClientPort`
backends:

* :class:`TavilyClient` — paid, LLM-friendly full-web API.
* :class:`SearXNGSearchClient` — key-free, self-hosted full-web search.
* :class:`DuckDuckGoSearchClient` — key-free Instant Answer only
  (limited coverage, clearly labelled in the UI).
* :class:`OpenAIWebSearchClient` — OpenAI Responses API built-in web
  search; the model searches + synthesises server-side and returns a
  fused answer plus citations.

All adapters are HTTP-only — no dependency beyond ``httpx`` which is
already pulled in for the OpenAI-compatible LLM client.

The Tavily-prefixed names (``TavilyWebSearchTool``, ``TavilyError``,
``TavilyClientPort``, ``TavilySearchResponse``, ``TavilySearchResult``)
remain exported as back-compat aliases.
"""

from kokoro_link.infrastructure.tools.websearch.tool import (
    SearchClientPort,
    SearchError,
    SearchResponse,
    SearchResult,
    WebSearchTool,
    # Back-compat aliases.
    TavilyClientPort,
    TavilyError,
    TavilySearchResponse,
    TavilySearchResult,
    TavilyWebSearchTool,
)
from kokoro_link.infrastructure.tools.websearch.tavily_client import TavilyClient
from kokoro_link.infrastructure.tools.websearch.searxng_client import (
    SearXNGSearchClient,
)
from kokoro_link.infrastructure.tools.websearch.duckduckgo_client import (
    DuckDuckGoSearchClient,
)
from kokoro_link.infrastructure.tools.websearch.openai_web_search_client import (
    OpenAIWebSearchClient,
)

__all__ = [
    "WebSearchTool",
    "SearchClientPort",
    "SearchError",
    "SearchResponse",
    "SearchResult",
    "TavilyClient",
    "SearXNGSearchClient",
    "DuckDuckGoSearchClient",
    "OpenAIWebSearchClient",
    # Back-compat aliases.
    "TavilyWebSearchTool",
    "TavilyClientPort",
    "TavilyError",
    "TavilySearchResponse",
    "TavilySearchResult",
]
