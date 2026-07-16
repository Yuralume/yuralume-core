"""BDD for Telegram outbound image attachments.

Covers the routing inside ``TelegramAdapter.send`` when
``OutboundMessage.attachments`` is non-empty:

- single image + short text → one ``sendPhoto`` with caption, no text msg
- single image + long text (> caption limit) → text msg + photo w/o caption
- multiple images → first carries caption, rest use their own captions
- non-image attachment → degrades to a text note with the URL
- empty text + no attachments → no request sent
"""

from __future__ import annotations

import httpx
import pytest

from kokoro_link.contracts.messaging import (
    OutboundAttachment,
    OutboundMessage,
)
from kokoro_link.domain.value_objects.platform import Platform
from kokoro_link.infrastructure.messaging.telegram.adapter import TelegramAdapter


_FAKE_IMAGE_BYTES = b"\x89PNG\r\n\x1a\nFAKE"
"""Tiny PNG-ish blob so tests can assert the multipart body includes it."""


def _ok_transport(
    captured: list[httpx.Request],
    image_bytes: bytes = _FAKE_IMAGE_BYTES,
) -> httpx.MockTransport:
    """Route image URLs → binary response; Telegram API → JSON OK.

    The adapter now does a self-fetch of the image URL before uploading
    multipart to Telegram, so the transport has to serve both roles.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if request.url.host == "api.telegram.org":
            return httpx.Response(200, json={"ok": True, "result": {}})
        # Any other GET is an image self-fetch.
        return httpx.Response(
            200,
            content=image_bytes,
            headers={"content-type": "image/png"},
        )

    return httpx.MockTransport(handler)


def _tg_requests(captured: list[httpx.Request]) -> list[httpx.Request]:
    return [r for r in captured if r.url.host == "api.telegram.org"]


def _outbound(**overrides: object) -> OutboundMessage:
    defaults: dict[str, object] = {
        "platform": Platform.TELEGRAM,
        "chat_ref": "99",
        "text": "",
        "credentials": {"bot_token": "TOKEN"},
        "attachments": (),
    }
    defaults.update(overrides)
    return OutboundMessage(**defaults)  # type: ignore[arg-type]


def _image(url: str, caption: str | None = None) -> OutboundAttachment:
    return OutboundAttachment(
        kind="image", url=url, mime_type="image/png", caption=caption,
    )


@pytest.mark.asyncio
async def test_single_image_with_short_text_uses_caption_only() -> None:
    captured: list[httpx.Request] = []
    adapter = TelegramAdapter(transport=_ok_transport(captured))

    await adapter.send(_outbound(
        text="這是現在的我～",
        attachments=(_image("https://ex.com/a.png"),),
    ))

    tg = _tg_requests(captured)
    assert len(tg) == 1  # no extra sendMessage
    req = tg[0]
    assert req.url.path == "/botTOKEN/sendPhoto"
    # Multipart body: search raw bytes rather than parsing boundaries.
    body = req.content
    assert b"name=\"caption\"" in body
    assert "現在的我".encode() in body
    # Image bytes themselves are embedded as the ``photo`` part.
    assert _FAKE_IMAGE_BYTES in body
    # The URL is NOT sent to Telegram anymore — we upload bytes directly.
    assert b"https://ex.com/a.png" not in body


@pytest.mark.asyncio
async def test_long_text_sent_separately_then_photo_without_caption() -> None:
    captured: list[httpx.Request] = []
    adapter = TelegramAdapter(transport=_ok_transport(captured))

    long_text = "x" * 2000
    await adapter.send(_outbound(
        text=long_text,
        attachments=(_image("https://ex.com/a.png"),),
    ))

    tg = _tg_requests(captured)
    assert len(tg) == 2
    assert tg[0].url.path == "/botTOKEN/sendMessage"
    assert tg[1].url.path == "/botTOKEN/sendPhoto"
    photo_body = tg[1].content
    # No caption field in the photo multipart body
    assert b"name=\"caption\"" not in photo_body


@pytest.mark.asyncio
async def test_multiple_images_first_gets_text_rest_keep_own_caption() -> None:
    captured: list[httpx.Request] = []
    adapter = TelegramAdapter(transport=_ok_transport(captured))

    await adapter.send(_outbound(
        text="兩張一起看",
        attachments=(
            _image("https://ex.com/a.png"),
            _image("https://ex.com/b.png", caption="第二張"),
        ),
    ))

    tg = _tg_requests(captured)
    # No separate sendMessage — short text fits caption budget.
    assert len(tg) == 2
    assert tg[0].url.path == "/botTOKEN/sendPhoto"
    assert tg[1].url.path == "/botTOKEN/sendPhoto"
    assert "兩張一起看".encode() in tg[0].content
    assert "第二張".encode() in tg[1].content


@pytest.mark.asyncio
async def test_text_only_still_uses_sendMessage() -> None:
    captured: list[httpx.Request] = []
    adapter = TelegramAdapter(transport=_ok_transport(captured))

    await adapter.send(_outbound(text="hello", attachments=()))

    assert len(captured) == 1
    assert captured[0].url.path == "/botTOKEN/sendMessage"


@pytest.mark.asyncio
async def test_empty_text_and_no_attachments_still_calls_send_message() -> None:
    """Explicit: pure empty messages go through sendMessage with an
    empty body. We rely on Telegram to reject; not the adapter's job
    to filter. Verifies we don't silently swallow."""
    captured: list[httpx.Request] = []
    adapter = TelegramAdapter(transport=_ok_transport(captured))

    await adapter.send(_outbound(text="", attachments=()))

    # Zero attachments + empty text → the adapter takes the "leftover
    # text is empty" branch AND the images loop is empty AND there
    # are no ``others``, so it issues nothing. This documents that
    # behaviour explicitly.
    assert captured == []


@pytest.mark.asyncio
async def test_non_image_attachment_falls_through_as_text_note() -> None:
    captured: list[httpx.Request] = []
    adapter = TelegramAdapter(transport=_ok_transport(captured))

    await adapter.send(_outbound(
        text="看這個",
        attachments=(
            OutboundAttachment(
                kind="file", url="https://ex.com/doc.pdf", caption="報告",
            ),
        ),
    ))

    # Non-image attachments do NOT trigger a fetch — just text note.
    assert len(captured) == 2
    assert captured[0].url.path == "/botTOKEN/sendMessage"
    assert captured[1].url.path == "/botTOKEN/sendMessage"
    body = captured[1].content.decode()
    assert "報告" in body


@pytest.mark.asyncio
async def test_other_attachments_header_localized_for_en_operator() -> None:
    """The '(其他附件)' header is deterministic channel text; an en-US
    operator must get the English header, not zh-TW."""
    captured: list[httpx.Request] = []
    adapter = TelegramAdapter(transport=_ok_transport(captured))

    await adapter.send(_outbound(
        text="",
        locale="en-US",
        attachments=(
            OutboundAttachment(
                kind="file", url="https://ex.com/doc.pdf", caption="report",
            ),
        ),
    ))

    tg = _tg_requests(captured)
    assert len(tg) == 1
    body = tg[0].content.decode()
    assert "其他附件" not in body
    assert "Other attachments" in body
    # The caption itself is still included in the list.
    assert "report" in body


# --- Multipart upload behaviour (replaces old URL-based sendPhoto) ---


@pytest.mark.asyncio
async def test_image_is_self_fetched_then_multipart_uploaded() -> None:
    """Adapter GETs the image URL itself before POSTing to Telegram.

    Rationale: Telegram's URL fetcher has a ~5s timeout + separate
    network path that sometimes fails even for URLs reachable from a
    browser (``failed to get HTTP URL content``). Self-fetching and
    forwarding as multipart sidesteps that entirely.
    """
    captured: list[httpx.Request] = []
    adapter = TelegramAdapter(transport=_ok_transport(captured))

    await adapter.send(_outbound(
        text="看我",
        attachments=(_image("https://my-server.test/uploads/a.png"),),
    ))

    # Two requests: self-fetch (GET) then Telegram upload (POST multipart).
    assert len(captured) == 2
    fetch, upload = captured[0], captured[1]

    assert fetch.method == "GET"
    assert str(fetch.url) == "https://my-server.test/uploads/a.png"

    assert upload.method == "POST"
    assert upload.url.host == "api.telegram.org"
    assert upload.url.path == "/botTOKEN/sendPhoto"
    # Confirm this really is multipart, not JSON
    content_type = upload.headers.get("content-type", "")
    assert content_type.startswith("multipart/form-data")
    # Image bytes are in the body
    assert _FAKE_IMAGE_BYTES in upload.content
    # Filename derived from URL
    assert b"filename=\"a.png\"" in upload.content


@pytest.mark.asyncio
async def test_local_image_fetcher_bypasses_public_http_get() -> None:
    captured: list[httpx.Request] = []
    fetched_urls: list[str] = []

    async def local_fetcher(url: str) -> bytes | None:
        fetched_urls.append(url)
        return _FAKE_IMAGE_BYTES

    adapter = TelegramAdapter(
        transport=_ok_transport(captured),
        local_image_fetcher=local_fetcher,
    )

    await adapter.send(_outbound(
        text="看我",
        attachments=(
            _image("https://public.example.test/v1/public/characters/a.png"),
        ),
    ))

    assert fetched_urls == [
        "https://public.example.test/v1/public/characters/a.png",
    ]
    assert len(captured) == 1
    assert captured[0].url.host == "api.telegram.org"
    assert captured[0].url.path == "/botTOKEN/sendPhoto"
    assert _FAKE_IMAGE_BYTES in captured[0].content


@pytest.mark.asyncio
async def test_image_fetch_404_skips_upload(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Dangling image URL → we log + skip, don't crash the whole send."""

    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if request.url.host == "api.telegram.org":
            return httpx.Response(200, json={"ok": True, "result": {}})
        return httpx.Response(404, text="not found")

    adapter = TelegramAdapter(transport=httpx.MockTransport(handler))

    with caplog.at_level("WARNING"):
        await adapter.send(_outbound(
            text="",
            attachments=(_image("https://my-server.test/uploads/gone.png"),),
        ))

    # Only the failed GET was captured — no sendPhoto attempt.
    assert [(r.method, r.url.host) for r in captured] == [
        ("GET", "my-server.test"),
    ]
    assert any(
        "image fetch failed" in r.message for r in caplog.records
    )


@pytest.mark.asyncio
async def test_oversized_image_skipped_without_upload(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Telegram sendPhoto caps at 10 MB — bigger blobs would error-out
    server-side with a confusing message. We pre-flight the size and
    skip cleanly."""

    huge_bytes = b"x" * (11 * 1024 * 1024)  # 11 MB
    captured: list[httpx.Request] = []
    adapter = TelegramAdapter(transport=_ok_transport(captured, huge_bytes))

    with caplog.at_level("WARNING"):
        await adapter.send(_outbound(
            text="",
            attachments=(_image("https://my-server.test/big.png"),),
        ))

    # Only the fetch was made; no upload to TG
    assert len(captured) == 1
    assert captured[0].method == "GET"
    assert any(
        "exceeds 10 MB" in r.message for r in caplog.records
    )


@pytest.mark.asyncio
async def test_oversized_dimensions_degrade_to_sendDocument(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A 9000×2000 PNG (sum > 10000) → Telegram rejects sendPhoto. We
    pre-probe and route it through sendDocument instead so delivery
    still succeeds."""
    import struct

    # Hand-rolled minimum PNG header for 9000×2000
    giant_png = (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13)
        + b"IHDR"
        + struct.pack(">II", 9000, 2000)
        + b"\x08\x02\x00\x00\x00"
    )

    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if request.url.host == "api.telegram.org":
            return httpx.Response(200, json={"ok": True, "result": {}})
        return httpx.Response(
            200, content=giant_png, headers={"content-type": "image/png"},
        )

    adapter = TelegramAdapter(transport=httpx.MockTransport(handler))
    with caplog.at_level("WARNING"):
        await adapter.send(_outbound(
            text="",
            attachments=(_image("https://my-server.test/huge.png"),),
        ))

    tg = _tg_requests(captured)
    assert len(tg) == 1
    # Endpoint switched from sendPhoto to sendDocument
    assert tg[0].url.path == "/botTOKEN/sendDocument"
    # Multipart field renamed accordingly
    assert b"name=\"document\"" in tg[0].content
    assert b"name=\"photo\"" not in tg[0].content
    # Warning log tells ops why
    assert any(
        "sending as document" in r.message for r in caplog.records
    )


@pytest.mark.asyncio
async def test_multipart_uses_declared_mime_not_response_content_type() -> None:
    """Some static servers mis-label PNGs as octet-stream; we trust the
    DTO's declared mime instead of the fetch response's Content-Type."""

    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if request.url.host == "api.telegram.org":
            return httpx.Response(200, json={"ok": True, "result": {}})
        # Deliberately wrong content-type from the fetch
        return httpx.Response(
            200,
            content=_FAKE_IMAGE_BYTES,
            headers={"content-type": "application/octet-stream"},
        )

    adapter = TelegramAdapter(transport=httpx.MockTransport(handler))
    await adapter.send(_outbound(
        text="",
        attachments=(
            OutboundAttachment(
                kind="image",
                url="https://my-server.test/a.png",
                mime_type="image/png",
                caption=None,
            ),
        ),
    ))

    tg = _tg_requests(captured)
    assert len(tg) == 1
    # Multipart should declare image/png (from DTO), not octet-stream
    assert b"Content-Type: image/png" in tg[0].content
