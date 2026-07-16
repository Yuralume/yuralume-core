"""Web-page fetching port.

Splitting this out from ``contracts/tool.py`` lets us swap the
concrete strategy (httpx + readability today, headless browser via
agent-browser tomorrow) without touching the tool wrapper or the
orchestrator. ``WebFetchTool`` depends only on ``WebFetchPort``, so a
deployment that wants full JS rendering just wires a different
adapter in ``bootstrap/container.py``.

The port is deliberately minimal — one method, one result shape —
because every "fetch a URL and extract the readable text" strategy
can be made to fit. Anything richer (authentication, per-site
rules, cookie jars) is the adapter's concern, not the contract's.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class WebFetchResult:
    """Normalized output of a fetch.

    ``url`` is the *final* URL after any redirects — callers display
    this as the citation source. ``title`` / ``text`` are the
    extracted main content; adapters are responsible for stripping
    navigation / ads / scripts. ``truncated`` signals the text has
    been cut at the adapter's length cap so the tool layer can hint
    to the LLM that there's more if it re-fetches a narrower scope.
    """

    url: str
    title: str
    text: str
    truncated: bool = False


class WebFetchError(RuntimeError):
    """Raised when a fetch fails for any reason the caller can't
    recover from (DNS miss, HTTP error, timeout, unparseable body).

    The tool layer catches this and turns it into a
    ``ToolResult.failure`` so the chat loop keeps flowing.
    """


class WebFetchPort(Protocol):
    async def fetch(self, url: str) -> WebFetchResult: ...
