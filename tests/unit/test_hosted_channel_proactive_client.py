"""Transport-classification lock for HostedChannelProactiveClient (LH4/LH7-D).

Each Channel status code maps to exactly one ledger-facing outcome: 2xx/404/409
for accept, and 429/5xx/network for the retryable transient error.

Eligibility is the one route where the status code is NOT the whole answer: the
Channel answers ``200`` for both verdicts and puts the verdict in the body's
``eligible`` field (LH7-D). So the eligibility cases below pin the BODY
semantics — declined ⇒ ``False`` with the ``reason`` logged, an unreadable body
⇒ the pre-LH7-D status-only semantics (``True``) plus a loud warning so a new
Core against an old Channel keeps pushing instead of going silent.

Service-auth headers (caller=core, audience=yuralume-channel) ride every
request.
"""

from __future__ import annotations

import logging

import httpx
import pytest

from kokoro_link.infrastructure.cloud.hosted_channel_proactive_client import (
    ACCEPT_ACCEPTED,
    ACCEPT_CONFLICT,
    ACCEPT_NO_ENDPOINT,
    ChannelDeliveryTransientError,
    HostedChannelProactiveClient,
)

_CREDENTIAL = (
    "core-kid|core|yuralume-channel|"
    "delivery:eligibility-read,delivery:create|s3cret"
)


class _MockAsyncClient(httpx.AsyncClient):
    def __init__(self, handler, **kwargs) -> None:
        super().__init__(
            transport=httpx.MockTransport(handler),
            base_url=kwargs["base_url"],
            timeout=kwargs["timeout"],
        )


def _install(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: _MockAsyncClient(handler, **kwargs),
    )


def _client() -> HostedChannelProactiveClient:
    return HostedChannelProactiveClient(
        base_url="https://channel.example/",
        service_credential=_CREDENTIAL,
    )


def _envelope_payload() -> dict[str, object]:
    return {
        "event_id": "evt-1",
        "tenant_id": "t1",
        "account_id": "a1",
        "character_id": "c1",
        "kind": "proactive",
        "segments": [{"text": "hi"}],
        "attachments": [],
        "locale": "zh-TW",
        "created_at": "2026-07-24T09:00:00+00:00",
        "expires_at": "2026-07-24T10:00:00+00:00",
        "contract_version": 1,
    }


@pytest.mark.asyncio
async def test_eligibility_200_eligible_true_with_path_and_auth_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["caller"] = request.headers.get("X-Yuralume-Service-Caller")
        seen["audience"] = request.headers.get("X-Yuralume-Service-Audience")
        return httpx.Response(
            200,
            json={"eligible": True, "ttl_seconds": 60, "reason": None},
        )

    _install(monkeypatch, handler)
    verdict = await _client().get_eligibility(
        tenant_id="t1", account_id="a1", character_id="c1",
    )
    assert verdict.eligible is True
    assert verdict.reason is None
    assert seen["url"] == (
        "https://channel.example/internal/v1/delivery-eligibility/t1/a1/c1"
    )
    assert seen["caller"] == "core"
    assert seen["audience"] == "yuralume-channel"


@pytest.mark.asyncio
async def test_eligibility_200_eligible_false_is_false_and_logs_reason(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """A 200 that says "no" must not be read as "yes" (LH7-D).

    This is the whole point of the ticket: the character is simply not opted in,
    and Core must skip it BEFORE spending decider/generation budget. The reason
    reaches the log so an operator can see *why* nothing was pushed.
    """
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "eligible": False,
                "ttl_seconds": 30,
                "reason": "not_opted_in",
            },
        )

    _install(monkeypatch, handler)
    with caplog.at_level(logging.INFO):
        verdict = await _client().get_eligibility(
            tenant_id="t1", account_id="a1", character_id="c1",
        )
    assert verdict.eligible is False
    # LQ-F2: the channel's own reason rides the verdict verbatim, not just the
    # log — the LINE reactivation candidate listing surfaces it to an operator.
    assert verdict.reason == "not_opted_in"
    assert "not_opted_in" in caplog.text
    # Module logging discipline: identifiers never reach this module's log —
    # correlation lives server-side in the channel's delivery ledger. (Scoped
    # to our records: httpx's own request log legitimately carries the URL.)
    own = " ".join(
        record.getMessage() for record in caplog.records
        if record.name.endswith("hosted_channel_proactive_client")
    )
    assert own
    assert "t1" not in own
    assert "a1" not in own
    assert "c1" not in own


@pytest.mark.asyncio
async def test_eligibility_200_quota_exhausted_reason_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LQ-F2: a quota decline must read as a quota decline downstream.

    Before LQ-F2 every decline collapsed to the same generic string by the
    time it reached the LINE reactivation candidate listing, so an operator
    facing a merely-exhausted-quota character had no way to tell it apart
    from a genuinely broken binding. The channel's specific reason string
    must survive the client layer untouched.
    """
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "eligible": False,
                "ttl_seconds": 30,
                "reason": "quota_character_monthly_exhausted",
            },
        )

    _install(monkeypatch, handler)
    verdict = await _client().get_eligibility(
        tenant_id="t1", account_id="a1", character_id="c1",
    )
    assert verdict.eligible is False
    assert verdict.reason == "quota_character_monthly_exhausted"


@pytest.mark.asyncio
async def test_eligibility_200_declined_without_reason_still_false(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"eligible": False})

    _install(monkeypatch, handler)
    with caplog.at_level(logging.INFO):
        verdict = await _client().get_eligibility(
            tenant_id="t1", account_id="a1", character_id="c1",
        )
    assert verdict.eligible is False
    # The log line still says "unspecified" (existing log discipline), but the
    # propagated verdict carries None so callers apply their OWN fallback text
    # instead of "unspecified" leaking into a consumer-facing surface.
    assert verdict.reason is None
    assert "unspecified" in caplog.text


@pytest.mark.asyncio
async def test_eligibility_200_missing_field_keeps_legacy_true_with_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """Mixed-version window: new Core, old Channel that omits ``eligible``.

    A contract gap must not escalate into a push outage — keep the pre-LH7-D
    status-only semantics and make the mismatch loud instead.
    """
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ttl_seconds": 30})

    _install(monkeypatch, handler)
    with caplog.at_level(logging.WARNING):
        verdict = await _client().get_eligibility(
            tenant_id="t1", account_id="a1", character_id="c1",
        )
    assert verdict.eligible is True
    assert verdict.reason is None
    assert any(
        record.levelno == logging.WARNING for record in caplog.records
    )
    assert "eligible" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    ["", "not json at all", "[]", '{"eligible": "false"}'],
    ids=["empty", "malformed", "not-an-object", "non-boolean"],
)
async def test_eligibility_200_unreadable_body_keeps_legacy_true_with_warning(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    body: str,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=body.encode(), headers={
                "Content-Type": "application/json",
            },
        )

    _install(monkeypatch, handler)
    with caplog.at_level(logging.WARNING):
        verdict = await _client().get_eligibility(
            tenant_id="t1", account_id="a1", character_id="c1",
        )
    assert verdict.eligible is True
    assert any(
        record.levelno == logging.WARNING for record in caplog.records
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    [
        b'{"eligible": NaN}',
        b'{"eligible": Infinity}',
        b"[" * 6000 + b"]" * 6000,
    ],
    ids=["nan-constant", "infinity-constant", "deeply-nested"],
)
async def test_eligibility_200_hostile_body_degrades_to_legacy_true(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    content: bytes,
) -> None:
    """Non-standard JSON constants, decoder-depth attacks and broken encodings
    are all "unusable body": the documented degraded path (True + warning),
    never a crash out of the decoder and never a verdict read off a body the
    contract does not allow."""
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=content, headers={"Content-Type": "application/json"},
        )

    _install(monkeypatch, handler)
    with caplog.at_level(logging.WARNING):
        verdict = await _client().get_eligibility(
            tenant_id="t1", account_id="a1", character_id="c1",
        )
    assert verdict.eligible is True
    assert any(
        record.levelno == logging.WARNING for record in caplog.records
    )


@pytest.mark.asyncio
async def test_eligibility_200_invalid_utf8_reason_does_not_crash(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """Broken encodings never crash the tick. httpx's tolerant charset
    fallback decodes the bytes, so the unambiguous ``eligible: false`` verdict
    still lands — the mojibake only ever reaches the log through the collapsed
    and clamped reason path."""
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b'{"eligible": false, "reason": "\xff\xfe broken"}',
            headers={"Content-Type": "application/json"},
        )

    _install(monkeypatch, handler)
    with caplog.at_level(logging.INFO):
        verdict = await _client().get_eligibility(
            tenant_id="t1", account_id="a1", character_id="c1",
        )
    assert verdict.eligible is False
    declines = [
        record.getMessage() for record in caplog.records
        if "eligibility declined" in record.getMessage()
    ]
    assert len(declines) == 1
    assert "\n" not in declines[0]


@pytest.mark.asyncio
async def test_eligibility_declined_reason_is_collapsed_to_one_log_line(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """A channel-supplied string never gets to forge extra log lines."""
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"eligible": False, "reason": "not\n opted\r\nin " + "x" * 400},
        )

    _install(monkeypatch, handler)
    with caplog.at_level(logging.INFO):
        verdict = await _client().get_eligibility(
            tenant_id="t1", account_id="a1", character_id="c1",
        )
    assert verdict.eligible is False
    declines = [
        record for record in caplog.records
        if "eligibility declined" in record.getMessage()
    ]
    assert len(declines) == 1
    message = declines[0].getMessage()
    assert "\n" not in message
    assert "\r" not in message
    # Pin the exact normalization rule: whitespace collapsed to single spaces,
    # then clamped to 120 characters — not merely "no CR/LF, not 400 x's".
    expected = ("not opted in " + "x" * 400)[:120]
    reason = message.split("reason=", 1)[1].split(" — ", 1)[0]
    assert reason == expected
    assert len(reason) <= 120
    # The same normalized/clamped text is what the verdict propagates —
    # log line and consumer-facing reason must never disagree (LQ-F2).
    assert verdict.reason == expected


@pytest.mark.asyncio
async def test_eligibility_404_false(monkeypatch: pytest.MonkeyPatch) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    _install(monkeypatch, handler)
    verdict = await _client().get_eligibility(
        tenant_id="t1", account_id="a1", character_id="c1",
    )
    assert verdict.eligible is False
    # A bare 404 carries no body — the client never invents a reason string;
    # the adapter layer supplies the consumer-facing fallback text (LQ-F2).
    assert verdict.reason is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [429, 500, 502, 503])
async def test_eligibility_transient_statuses_raise(
    monkeypatch: pytest.MonkeyPatch, status: int,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status)

    _install(monkeypatch, handler)
    with pytest.raises(ChannelDeliveryTransientError):
        await _client().get_eligibility(
            tenant_id="t1", account_id="a1", character_id="c1",
        )


@pytest.mark.asyncio
async def test_eligibility_network_error_raises_transient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    _install(monkeypatch, handler)
    with pytest.raises(ChannelDeliveryTransientError):
        await _client().get_eligibility(
            tenant_id="t1", account_id="a1", character_id="c1",
        )


@pytest.mark.asyncio
async def test_accept_202_accepted_with_delivery_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["scope"] = request.headers.get("X-Yuralume-Service-Scope")
        return httpx.Response(
            202,
            json={
                "delivery_id": "d-9",
                "event_id": "evt-1",
                "status": "queued",
            },
        )

    _install(monkeypatch, handler)
    result = await _client().accept_delivery(_envelope_payload())
    assert result.status == ACCEPT_ACCEPTED
    assert result.delivery_id == "d-9"
    assert seen["url"] == "https://channel.example/internal/v1/deliveries"
    assert "delivery:create" in str(seen["scope"])


@pytest.mark.asyncio
async def test_accept_200_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"delivery_id": "d-1"})

    _install(monkeypatch, handler)
    result = await _client().accept_delivery(_envelope_payload())
    assert result.status == ACCEPT_ACCEPTED
    assert result.delivery_id == "d-1"


@pytest.mark.asyncio
async def test_accept_404_no_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"code": "no_endpoint"}})

    _install(monkeypatch, handler)
    result = await _client().accept_delivery(_envelope_payload())
    assert result.status == ACCEPT_NO_ENDPOINT
    assert result.delivery_id is None


@pytest.mark.asyncio
async def test_accept_409_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"error": {"code": "idempotency"}})

    _install(monkeypatch, handler)
    result = await _client().accept_delivery(_envelope_payload())
    assert result.status == ACCEPT_CONFLICT


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [429, 500, 503])
async def test_accept_transient_statuses_raise(
    monkeypatch: pytest.MonkeyPatch, status: int,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status)

    _install(monkeypatch, handler)
    with pytest.raises(ChannelDeliveryTransientError):
        await _client().accept_delivery(_envelope_payload())


@pytest.mark.asyncio
async def test_accept_network_error_raises_transient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    _install(monkeypatch, handler)
    with pytest.raises(ChannelDeliveryTransientError):
        await _client().accept_delivery(_envelope_payload())
