import json

import httpx
import pytest

from kokoro_link.contracts.messaging import OutboundMessage
from kokoro_link.domain.value_objects.platform import Platform
from kokoro_link.infrastructure.messaging.line.adapter import LineAdapter


def _ok_transport(captured: list[httpx.Request]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={})

    return httpx.MockTransport(handler)


def _outbound(**overrides: object) -> OutboundMessage:
    defaults: dict[str, object] = {
        "platform": Platform.LINE,
        "chat_ref": "U1",
        "text": "hi",
        "credentials": {"channel_access_token": "AT"},
    }
    defaults.update(overrides)
    return OutboundMessage(**defaults)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_send_uses_credentials_from_message() -> None:
    captured: list[httpx.Request] = []
    adapter = LineAdapter(transport=_ok_transport(captured))

    await adapter.send(_outbound())

    assert len(captured) == 1
    request = captured[0]
    assert request.url.path == "/v2/bot/message/push"
    assert request.headers["Authorization"] == "Bearer AT"
    body = json.loads(request.content.decode())
    assert body == {"to": "U1", "messages": [{"type": "text", "text": "hi"}]}


@pytest.mark.asyncio
async def test_send_uses_per_call_credentials() -> None:
    captured: list[httpx.Request] = []
    adapter = LineAdapter(transport=_ok_transport(captured))

    await adapter.send(_outbound(credentials={"channel_access_token": "alpha"}))
    await adapter.send(_outbound(credentials={"channel_access_token": "beta"}))

    assert [r.headers["Authorization"] for r in captured] == [
        "Bearer alpha", "Bearer beta",
    ]


@pytest.mark.asyncio
async def test_send_skips_silently_on_missing_token(
    caplog: pytest.LogCaptureFixture,
) -> None:
    adapter = LineAdapter()
    with caplog.at_level("WARNING"):
        await adapter.send(_outbound(credentials={}))
    assert any(
        "missing channel_access_token" in r.message for r in caplog.records
    )


@pytest.mark.asyncio
async def test_send_rejects_mismatched_platform() -> None:
    adapter = LineAdapter()
    with pytest.raises(ValueError):
        await adapter.send(_outbound(platform=Platform.TELEGRAM))


@pytest.mark.asyncio
async def test_send_swallows_4xx(caplog: pytest.LogCaptureFixture) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "Authentication failed"})

    adapter = LineAdapter(transport=httpx.MockTransport(handler))
    with caplog.at_level("WARNING"):
        await adapter.send(_outbound())
    assert any("LINE push failed" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_send_swallows_transport_error(caplog: pytest.LogCaptureFixture) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    adapter = LineAdapter(transport=httpx.MockTransport(handler))
    with caplog.at_level("ERROR"):
        await adapter.send(_outbound())
    assert any(
        "LINE push transport error" in r.message for r in caplog.records
    )


# ---------- reply-first delivery (cost optimisation) ----------


@pytest.mark.asyncio
async def test_reply_token_uses_free_reply_api() -> None:
    """Inbound-triggered replies must ride the free reply API — a push
    here would burn the monthly quota for nothing."""
    captured: list[httpx.Request] = []
    adapter = LineAdapter(transport=_ok_transport(captured))

    await adapter.send(_outbound(reply_context={"reply_token": "r-1"}))

    assert len(captured) == 1
    request = captured[0]
    assert request.url.path == "/v2/bot/message/reply"
    assert request.headers["Authorization"] == "Bearer AT"
    body = json.loads(request.content.decode())
    assert body == {
        "replyToken": "r-1",
        "messages": [{"type": "text", "text": "hi"}],
    }


@pytest.mark.asyncio
async def test_reply_rejected_by_line_falls_back_to_push(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An expired / already-used replyToken makes LINE answer 400; the
    message must then go out via push instead of being lost."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if request.url.path == "/v2/bot/message/reply":
            return httpx.Response(400, json={"message": "Invalid reply token"})
        return httpx.Response(200, json={})

    adapter = LineAdapter(transport=httpx.MockTransport(handler))
    with caplog.at_level("INFO"):
        await adapter.send(_outbound(reply_context={"reply_token": "r-expired"}))

    assert [r.url.path for r in captured] == [
        "/v2/bot/message/reply",
        "/v2/bot/message/push",
    ]
    push_body = json.loads(captured[1].content.decode())
    assert push_body == {
        "to": "U1",
        "messages": [{"type": "text", "text": "hi"}],
    }
    assert any("falling back to push" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_reply_transport_error_does_not_push_duplicate(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When the reply call dies in transit we cannot know whether LINE
    delivered it — pushing again could double-send, so we only log."""
    attempts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request.url.path)
        raise httpx.ConnectError("boom", request=request)

    adapter = LineAdapter(transport=httpx.MockTransport(handler))
    with caplog.at_level("ERROR"):
        await adapter.send(_outbound(reply_context={"reply_token": "r-1"}))

    assert attempts == ["/v2/bot/message/reply"]
    assert any(
        "LINE reply transport error" in r.message for r in caplog.records
    )


@pytest.mark.asyncio
async def test_reply_server_error_does_not_push_duplicate(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A 5xx from LINE leaves delivery ambiguous — never double-send."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(500, json={"message": "internal"})

    adapter = LineAdapter(transport=httpx.MockTransport(handler))
    with caplog.at_level("WARNING"):
        await adapter.send(_outbound(reply_context={"reply_token": "r-1"}))

    assert [r.url.path for r in captured] == ["/v2/bot/message/reply"]
    assert any("LINE reply failed" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_empty_reply_context_goes_straight_to_push() -> None:
    """Proactive sends and post-first segments carry no reply token —
    they must keep using push without attempting a reply call."""
    captured: list[httpx.Request] = []
    adapter = LineAdapter(transport=_ok_transport(captured))

    await adapter.send(_outbound(reply_context={}))

    assert [r.url.path for r in captured] == ["/v2/bot/message/push"]


@pytest.mark.asyncio
async def test_reply_packs_text_and_image_like_push() -> None:
    """The 5-object packing logic must be shared between reply and push."""
    from kokoro_link.contracts.messaging import OutboundAttachment

    captured: list[httpx.Request] = []
    adapter = LineAdapter(transport=_ok_transport(captured))

    await adapter.send(_outbound(
        text="這張",
        reply_context={"reply_token": "r-1"},
        attachments=(
            OutboundAttachment(
                kind="image", url="https://ex.com/a.png",
                mime_type="image/png",
            ),
        ),
    ))

    assert len(captured) == 1
    body = json.loads(captured[0].content.decode())
    assert body["replyToken"] == "r-1"
    assert body["messages"] == [
        {"type": "text", "text": "這張"},
        {
            "type": "image",
            "originalContentUrl": "https://ex.com/a.png",
            "previewImageUrl": "https://ex.com/a.png",
        },
    ]


# ---------- batched reply (5-objects-per-call cost optimisation) ----------


def _bubbles(
    texts: list[str],
    *,
    reply_token: str = "",
    attachments_on_last: tuple = (),
) -> list[OutboundMessage]:
    """Mirror ``segment_outbound_message`` output: reply affinity on the
    first bubble, attachments on the last."""
    last_index = len(texts) - 1
    return [
        _outbound(
            text=text,
            reply_context=(
                {"reply_token": reply_token}
                if reply_token and index == 0 else {}
            ),
            attachments=(
                attachments_on_last if index == last_index else ()
            ),
        )
        for index, text in enumerate(texts)
    ]


@pytest.mark.asyncio
async def test_send_many_packs_bubbles_into_one_reply_call() -> None:
    """A multi-bubble inbound reply must ride ONE free reply call — one
    call per bubble would burn a push-quota unit for every bubble past
    the first."""
    captured: list[httpx.Request] = []
    adapter = LineAdapter(transport=_ok_transport(captured))

    await adapter.send_many(_bubbles(["一", "二", "三"], reply_token="r-1"))

    assert [r.url.path for r in captured] == ["/v2/bot/message/reply"]
    body = json.loads(captured[0].content.decode())
    assert body == {
        "replyToken": "r-1",
        "messages": [
            {"type": "text", "text": "一"},
            {"type": "text", "text": "二"},
            {"type": "text", "text": "三"},
        ],
    }


@pytest.mark.asyncio
async def test_send_many_overflow_beyond_five_pushes_remainder() -> None:
    """LINE takes at most 5 message objects per call: the first five
    bubbles ride the reply, the overflow goes out as ONE push (quota
    counts per call recipient, not per object)."""
    captured: list[httpx.Request] = []
    adapter = LineAdapter(transport=_ok_transport(captured))

    await adapter.send_many(
        _bubbles([f"第{i}則" for i in range(1, 8)], reply_token="r-1"),
    )

    assert [r.url.path for r in captured] == [
        "/v2/bot/message/reply",
        "/v2/bot/message/push",
    ]
    reply_body = json.loads(captured[0].content.decode())
    push_body = json.loads(captured[1].content.decode())
    assert [m["text"] for m in reply_body["messages"]] == [
        "第1則", "第2則", "第3則", "第4則", "第5則",
    ]
    assert push_body["to"] == "U1"
    assert [m["text"] for m in push_body["messages"]] == ["第6則", "第7則"]


@pytest.mark.asyncio
async def test_send_many_without_token_pushes_in_five_object_chunks() -> None:
    """Proactive multi-bubble sends carry no token but still batch:
    7 bubbles must cost 2 push calls, not 7."""
    captured: list[httpx.Request] = []
    adapter = LineAdapter(transport=_ok_transport(captured))

    await adapter.send_many(_bubbles([f"第{i}則" for i in range(1, 8)]))

    assert [r.url.path for r in captured] == [
        "/v2/bot/message/push",
        "/v2/bot/message/push",
    ]
    first = json.loads(captured[0].content.decode())
    second = json.loads(captured[1].content.decode())
    assert len(first["messages"]) == 5
    assert [m["text"] for m in second["messages"]] == ["第6則", "第7則"]


@pytest.mark.asyncio
async def test_send_many_reply_rejected_falls_back_to_push_for_all_chunks(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An expired token rejects the whole first chunk — it must be
    re-sent via push, and later chunks keep going out."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if request.url.path == "/v2/bot/message/reply":
            return httpx.Response(400, json={"message": "Invalid reply token"})
        return httpx.Response(200, json={})

    adapter = LineAdapter(transport=httpx.MockTransport(handler))
    with caplog.at_level("INFO"):
        await adapter.send_many(
            _bubbles([f"第{i}則" for i in range(1, 7)], reply_token="r-expired"),
        )

    assert [r.url.path for r in captured] == [
        "/v2/bot/message/reply",
        "/v2/bot/message/push",
        "/v2/bot/message/push",
    ]
    first_push = json.loads(captured[1].content.decode())
    second_push = json.loads(captured[2].content.decode())
    assert [m["text"] for m in first_push["messages"]] == [
        "第1則", "第2則", "第3則", "第4則", "第5則",
    ]
    assert [m["text"] for m in second_push["messages"]] == ["第6則"]
    assert any("falling back to push" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_send_many_reply_ambiguous_skips_first_chunk_but_pushes_rest(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A 5xx on the reply leaves the first chunk's delivery unknown —
    never re-send it. Later chunks were never attempted, so pushing
    them cannot double-deliver."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if request.url.path == "/v2/bot/message/reply":
            return httpx.Response(500, json={"message": "internal"})
        return httpx.Response(200, json={})

    adapter = LineAdapter(transport=httpx.MockTransport(handler))
    with caplog.at_level("WARNING"):
        await adapter.send_many(
            _bubbles([f"第{i}則" for i in range(1, 7)], reply_token="r-1"),
        )

    assert [r.url.path for r in captured] == [
        "/v2/bot/message/reply",
        "/v2/bot/message/push",
    ]
    push_body = json.loads(captured[1].content.decode())
    assert [m["text"] for m in push_body["messages"]] == ["第6則"]
    assert any("LINE reply failed" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_send_many_counts_attachment_objects_in_chunks() -> None:
    """Attachments on the last bubble are message objects too — they
    must participate in the 5-object arithmetic, not vanish."""
    from kokoro_link.contracts.messaging import OutboundAttachment

    captured: list[httpx.Request] = []
    adapter = LineAdapter(transport=_ok_transport(captured))

    await adapter.send_many(_bubbles(
        [f"第{i}則" for i in range(1, 6)],
        reply_token="r-1",
        attachments_on_last=(
            OutboundAttachment(
                kind="image", url="https://ex.com/a.png",
                mime_type="image/png",
            ),
        ),
    ))

    assert [r.url.path for r in captured] == [
        "/v2/bot/message/reply",
        "/v2/bot/message/push",
    ]
    push_body = json.loads(captured[1].content.decode())
    assert push_body["messages"] == [
        {
            "type": "image",
            "originalContentUrl": "https://ex.com/a.png",
            "previewImageUrl": "https://ex.com/a.png",
        },
    ]


@pytest.mark.asyncio
async def test_send_many_missing_token_warns_and_skips(
    caplog: pytest.LogCaptureFixture,
) -> None:
    adapter = LineAdapter()
    with caplog.at_level("WARNING"):
        await adapter.send_many(
            [_outbound(credentials={}), _outbound(credentials={})],
        )
    assert any(
        "missing channel_access_token" in r.message for r in caplog.records
    )


@pytest.mark.asyncio
async def test_send_many_rejects_mismatched_platform() -> None:
    adapter = LineAdapter()
    with pytest.raises(ValueError):
        await adapter.send_many(
            [_outbound(), _outbound(platform=Platform.TELEGRAM)],
        )


@pytest.mark.asyncio
async def test_send_many_with_nothing_sendable_makes_no_calls() -> None:
    captured: list[httpx.Request] = []
    adapter = LineAdapter(transport=_ok_transport(captured))

    await adapter.send_many([])
    await adapter.send_many([_outbound(text="")])

    assert captured == []


@pytest.mark.asyncio
async def test_set_webhook_endpoint_puts_url() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={})

    adapter = LineAdapter(transport=httpx.MockTransport(handler))
    result = await adapter.set_webhook_endpoint(
        channel_access_token="AT",
        webhook_url="https://example.test/hook",
    )

    assert result == {"ok": True}
    request = captured[0]
    assert request.method == "PUT"
    assert request.url.path == "/v2/bot/channel/webhook/endpoint"
    assert request.headers["Authorization"] == "Bearer AT"
    assert json.loads(request.content.decode()) == {
        "endpoint": "https://example.test/hook",
    }


@pytest.mark.asyncio
async def test_set_webhook_endpoint_reports_4xx() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "invalid token"})

    adapter = LineAdapter(transport=httpx.MockTransport(handler))
    result = await adapter.set_webhook_endpoint(
        channel_access_token="AT", webhook_url="https://example.test/h",
    )
    assert result["ok"] is False
    assert "401" in result["error"]


@pytest.mark.asyncio
async def test_get_webhook_endpoint_returns_payload() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        return httpx.Response(
            200,
            json={
                "endpoint": "https://example.test/hook",
                "active": True,
            },
        )

    adapter = LineAdapter(transport=httpx.MockTransport(handler))
    result = await adapter.get_webhook_endpoint(channel_access_token="AT")

    assert result["ok"] is True
    assert result["endpoint"] == "https://example.test/hook"
    assert result["active"] is True
