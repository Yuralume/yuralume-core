"""AN1 — Core-side HTTP client for the User service unread-announcements endpoint.

Mirrors ``test_cloud_credit_balance_client``: versioned service-credential
headers, the path/query contract, and the typed ``AnnouncementUnreadUnavailable``
mapping the cache relies on for fail-soft.
"""

from __future__ import annotations

import httpx
import pytest

from kokoro_link.contracts.cloud_announcements import (
    AnnouncementUnreadUnavailable,
)
from kokoro_link.infrastructure.cloud.announcement_unread_client import (
    AnnouncementUnreadClient,
)

_CREDENTIAL = (
    "core-kid|core|yuralume-user|announcements:read|core-secret"
)


class _MockAsyncClient(httpx.AsyncClient):
    def __init__(self, handler, **kwargs) -> None:
        super().__init__(
            transport=httpx.MockTransport(handler),
            base_url=kwargs["base_url"],
            timeout=kwargs["timeout"],
        )


def _install(monkeypatch, handler) -> None:
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: _MockAsyncClient(handler, **kwargs),
    )


def _client() -> AnnouncementUnreadClient:
    return AnnouncementUnreadClient(
        base_url="https://users.example/",
        internal_credential=_CREDENTIAL,
    )


def _responder(payload: object, status: int = 200):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload)

    return handler


@pytest.mark.asyncio
async def test_reads_the_unread_flag_with_its_own_scope(monkeypatch) -> None:
    seen: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["query"] = dict(request.url.params)
        seen["caller"] = request.headers.get("x-yuralume-service-caller")
        seen["audience"] = request.headers.get("x-yuralume-service-audience")
        seen["scope"] = request.headers.get("x-yuralume-service-scope")
        return httpx.Response(
            200,
            json={
                "has_unread": True,
                "unread_count": 2,
                "latest_published_at": "2026-07-29T00:00:00Z",
                "seen_at": None,
            },
        )

    _install(monkeypatch, handler)

    unread = await _client().fetch("tenant-1")

    assert seen["path"] == "/internal/v1/announcements/unread"
    assert seen["query"] == {"tenant_id": "tenant-1"}
    assert seen["caller"] == "core"
    assert seen["audience"] == "yuralume-user"
    # Its own scope, not the badge's: reading the board is not reading a wallet.
    assert seen["scope"] == "announcements:read"
    assert unread.has_unread is True
    assert unread.unread_count == 2
    assert unread.latest_published_at == "2026-07-29T00:00:00Z"


@pytest.mark.asyncio
async def test_a_caught_up_reader_is_a_valid_answer(monkeypatch) -> None:
    _install(
        monkeypatch,
        _responder({
            "has_unread": False,
            "unread_count": 0,
            "latest_published_at": None,
        }),
    )

    unread = await _client().fetch("tenant-1")

    assert unread.has_unread is False
    assert unread.unread_count == 0
    assert unread.latest_published_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"unread_count": 0},
        {"has_unread": None, "unread_count": 0},
        {"has_unread": "false", "unread_count": 0},
        {"has_unread": 0, "unread_count": 0},
    ],
)
async def test_an_unusable_flag_is_refused_not_read_as_silence(
    monkeypatch, payload,
) -> None:
    """Coercing a broken flag to ``False`` would cache a confident "nothing new".

    The dot would then stay dark over a notice the operator was required to
    give, and nothing would ever correct it. Refusing routes to the stale/503
    path, which says "we do not know".
    """
    _install(monkeypatch, _responder(payload))

    with pytest.raises(AnnouncementUnreadUnavailable):
        await _client().fetch("tenant-1")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"has_unread": True},
        {"has_unread": True, "unread_count": -1},
        {"has_unread": True, "unread_count": "2"},
        {"has_unread": True, "unread_count": True},
    ],
)
async def test_an_unusable_count_is_refused(monkeypatch, payload) -> None:
    _install(monkeypatch, _responder(payload))

    with pytest.raises(AnnouncementUnreadUnavailable):
        await _client().fetch("tenant-1")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"has_unread": False, "unread_count": 3},
        {"has_unread": True, "unread_count": 0},
    ],
)
async def test_a_flag_contradicting_its_count_is_refused(monkeypatch, payload) -> None:
    """Both fields state the same fact, so disagreement means partial upgrade or drift.

    Trusting either half caches a confident wrong state — and in the
    ``has_unread=False`` direction that state is "no notice for you". The sibling
    balance client refuses on the same grounds when the total contradicts its pools.
    """
    _install(monkeypatch, _responder(payload))

    with pytest.raises(AnnouncementUnreadUnavailable):
        await _client().fetch("tenant-1")


@pytest.mark.asyncio
async def test_non_2xx_maps_to_the_typed_failure(monkeypatch) -> None:
    _install(monkeypatch, _responder({"error": "nope"}, status=500))

    with pytest.raises(AnnouncementUnreadUnavailable):
        await _client().fetch("tenant-1")


@pytest.mark.asyncio
async def test_transport_failure_maps_to_the_typed_failure(monkeypatch) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    _install(monkeypatch, handler)

    with pytest.raises(AnnouncementUnreadUnavailable):
        await _client().fetch("tenant-1")


@pytest.mark.asyncio
async def test_a_non_object_body_is_refused(monkeypatch) -> None:
    _install(monkeypatch, _responder(["not", "an", "object"]))

    with pytest.raises(AnnouncementUnreadUnavailable):
        await _client().fetch("tenant-1")


@pytest.mark.asyncio
async def test_missing_configuration_never_reaches_the_network() -> None:
    with pytest.raises(AnnouncementUnreadUnavailable):
        await AnnouncementUnreadClient(base_url="").fetch("tenant-1")
    with pytest.raises(AnnouncementUnreadUnavailable):
        await _client().fetch("   ")
