"""Transport-classification lock for HostedChannelProactiveClient (LH4).

Each Channel status code maps to exactly one ledger-facing outcome: 200/404 for
eligibility, 2xx/404/409 for accept, and 429/5xx/network for the retryable
transient error. Service-auth headers (caller=core, audience=yuralume-channel)
ride every request.
"""

from __future__ import annotations

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
async def test_eligibility_200_true_with_path_and_auth_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["caller"] = request.headers.get("X-Yuralume-Service-Caller")
        seen["audience"] = request.headers.get("X-Yuralume-Service-Audience")
        return httpx.Response(200)

    _install(monkeypatch, handler)
    eligible = await _client().get_eligibility(
        tenant_id="t1", account_id="a1", character_id="c1",
    )
    assert eligible is True
    assert seen["url"] == (
        "https://channel.example/internal/v1/delivery-eligibility/t1/a1/c1"
    )
    assert seen["caller"] == "core"
    assert seen["audience"] == "yuralume-channel"


@pytest.mark.asyncio
async def test_eligibility_404_false(monkeypatch: pytest.MonkeyPatch) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    _install(monkeypatch, handler)
    eligible = await _client().get_eligibility(
        tenant_id="t1", account_id="a1", character_id="c1",
    )
    assert eligible is False


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
