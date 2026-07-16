"""Web-fetch tool package.

Default adapter is ``HttpxReadabilityFetcher`` — static-HTML focused,
zero runtime dependencies beyond ``httpx`` + ``readability-lxml``.
Future adapters (agent-browser, Playwright) plug in behind the same
``WebFetchPort`` contract without changing the ``WebFetchTool`` wrapper
or the container wiring site.
"""

from kokoro_link.infrastructure.tools.webfetch.httpx_fetcher import (
    HttpxReadabilityFetcher,
)
from kokoro_link.infrastructure.tools.webfetch.tool import WebFetchTool

__all__ = ["HttpxReadabilityFetcher", "WebFetchTool"]
