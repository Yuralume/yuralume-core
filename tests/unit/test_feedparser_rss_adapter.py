from __future__ import annotations

import logging
import ssl

import httpx
import pytest

from kokoro_link.contracts.rss_feed_fetcher import RssFetchError
from kokoro_link.infrastructure.world_event.feedparser_adapter import (
    FeedparserRssAdapter,
)

_URL = "https://feeds.example.test/world.xml?private=do-not-log"
_RSS = b"""<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"><channel><title>World</title>
  <item><title>First event</title><link>https://example.test/1</link>
    <description>Summary</description></item>
</channel></rss>"""


async def _fetch(adapter: FeedparserRssAdapter):
    return await adapter.fetch(
        source_id="source-a",
        source_name="Source A",
        feed_url=_URL,
        category="news",
        locale="en-US",
    )


@pytest.mark.asyncio
async def test_fetch_uses_http_status_before_parsing_error_page() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(410, text="<html><broken>", request=request)

    adapter = FeedparserRssAdapter(transport=httpx.MockTransport(handler))

    with pytest.raises(RssFetchError) as raised:
        await _fetch(adapter)

    assert raised.value.code == "http_status"
    assert raised.value.status_code == 410
    assert not raised.value.retryable
    assert calls == 1
    assert _URL not in str(raised.value)
    assert "private" not in str(raised.value)


@pytest.mark.asyncio
async def test_fetch_retries_transient_status_then_parses_success() -> None:
    statuses = iter((503, 200))

    def handler(request: httpx.Request) -> httpx.Response:
        status = next(statuses)
        return httpx.Response(
            status,
            content=_RSS if status == 200 else b"unavailable",
            headers={"content-type": "application/rss+xml"},
            request=request,
        )

    adapter = FeedparserRssAdapter(
        transport=httpx.MockTransport(handler), retry_attempts=2,
        retry_backoff_seconds=0,
    )

    events = await _fetch(adapter)

    assert [event.title for event in events] == ["First event"]


@pytest.mark.asyncio
async def test_fetch_retries_connect_timeout_then_succeeds() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectTimeout("slow", request=request)
        return httpx.Response(200, content=_RSS, request=request)

    adapter = FeedparserRssAdapter(
        transport=httpx.MockTransport(handler), retry_attempts=2,
        retry_backoff_seconds=0,
    )

    assert len(await _fetch(adapter)) == 1
    assert calls == 2


@pytest.mark.asyncio
async def test_tls_verification_failure_is_classified_and_not_retried() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        cause = ssl.SSLCertVerificationError("certificate verify failed")
        raise httpx.ConnectError(str(cause), request=request) from cause

    adapter = FeedparserRssAdapter(
        transport=httpx.MockTransport(handler), retry_attempts=3,
        retry_backoff_seconds=0,
    )

    with pytest.raises(RssFetchError) as raised:
        await _fetch(adapter)

    assert raised.value.code == "tls_certificate"
    assert not raised.value.retryable
    assert calls == 1


@pytest.mark.asyncio
async def test_malformed_xml_with_no_entries_is_an_explicit_failure() -> None:
    malformed = b"<rss><channel><item><title>broken</channel></rss>"
    adapter = FeedparserRssAdapter(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=malformed, request=request)
        )
    )

    with pytest.raises(RssFetchError) as raised:
        await _fetch(adapter)

    assert raised.value.code == "malformed_feed"


@pytest.mark.asyncio
async def test_html_body_with_200_is_not_silently_marked_as_empty_feed() -> None:
    adapter = FeedparserRssAdapter(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                text="<html><body>challenge page</body></html>",
                headers={"content-type": "text/html"},
                request=request,
            )
        )
    )

    with pytest.raises(RssFetchError) as raised:
        await _fetch(adapter)

    assert raised.value.code == "invalid_feed"


@pytest.mark.asyncio
async def test_partial_parse_and_dropped_entries_are_observable(
    caplog: pytest.LogCaptureFixture,
) -> None:
    partial = b"""<rss version="2.0"><channel>
      <item><title>usable</title><link>https://example.test/ok</link></item>
      <item><title>missing link</title></item>
      <broken>
    </channel></rss>"""
    adapter = FeedparserRssAdapter(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=partial, request=request)
        )
    )

    with caplog.at_level(logging.WARNING):
        events = await _fetch(adapter)

    assert [event.title for event in events] == ["usable"]
    assert "rss feed partially parsed source_id=source-a" in caplog.text
    assert "rss feed dropped invalid entries source_id=source-a count=1" in caplog.text
    assert _URL not in caplog.text


@pytest.mark.asyncio
async def test_oversized_response_is_rejected_before_parse() -> None:
    adapter = FeedparserRssAdapter(
        max_response_bytes=32,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=_RSS, request=request)
        ),
    )

    with pytest.raises(RssFetchError) as raised:
        await _fetch(adapter)

    assert raised.value.code == "response_too_large"


@pytest.mark.asyncio
async def test_redirect_chain_is_explicitly_bounded() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            302,
            headers={"location": f"https://redirect.test/{calls}"},
            request=request,
        )

    adapter = FeedparserRssAdapter(
        transport=httpx.MockTransport(handler),
        retry_attempts=1,
    )

    with pytest.raises(RssFetchError) as raised:
        await _fetch(adapter)

    assert raised.value.code == "redirect_limit"
    assert calls == 6  # initial request + five allowed redirects
