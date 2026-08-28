"""HostedChannelProactiveDeliveryAdapter behaviour lock (LH4, §8.3 / §4.4).

Covers eligibility TTL caching (eligible 60s / negative 30s / expiry re-query),
accept-outcome mapping to the delivery port's acceptance, transient propagation
(so the caller leaves the ledger pending), URL->AttachmentRef conversion with
unresolvable attachments skipped, and the defensive pre-send durability check.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from kokoro_link.application.services.proactive_delivery.hosted_adapter import (
    HostedChannelProactiveDeliveryAdapter,
)
from kokoro_link.contracts.external_proactive import (
    DeliveryAcceptanceStatus,
    ENVELOPE_KIND_PROACTIVE,
    ProactiveEnvelope,
    ProactiveSegment,
)
from kokoro_link.contracts.messaging import OutboundAttachment
from kokoro_link.contracts.object_storage import ObjectMetadata
from kokoro_link.infrastructure.cloud.hosted_channel_proactive_client import (
    ACCEPT_ACCEPTED,
    ACCEPT_CONFLICT,
    ACCEPT_NO_ENDPOINT,
    ChannelAcceptResult,
    ChannelDeliveryTransientError,
    EligibilityVerdict,
)
from kokoro_link.infrastructure.repositories.in_memory_external_proactive_events import (  # noqa: E501
    InMemoryExternalProactiveEventRepository,
)

_NOW = datetime(2026, 7, 24, 9, 0, tzinfo=timezone.utc)


class _FakeClient:
    def __init__(self, *, eligibility=None, accept=None) -> None:
        self._eligibility = eligibility
        self._accept = accept
        self.eligibility_calls = 0
        self.accept_payloads: list[dict] = []

    async def get_eligibility(self, *, tenant_id, account_id, character_id):
        self.eligibility_calls += 1
        if isinstance(self._eligibility, Exception):
            raise self._eligibility
        if isinstance(self._eligibility, EligibilityVerdict):
            return self._eligibility
        # Bare bool convenience for tests that don't care about the reason.
        return EligibilityVerdict(eligible=bool(self._eligibility), reason=None)

    async def accept_delivery(self, payload):
        self.accept_payloads.append(payload)
        if isinstance(self._accept, Exception):
            raise self._accept
        return self._accept


class _FakeStorage:
    def __init__(self) -> None:
        self._url_to_key: dict[str, str] = {}
        self._meta: dict[str, ObjectMetadata] = {}

    def add(self, *, url, key, content_type, size_bytes, sha256) -> None:
        self._url_to_key[url] = key
        self._meta[key] = ObjectMetadata(
            object_key=key,
            url=url,
            content_type=content_type,
            size_bytes=size_bytes,
            sha256=sha256,
        )

    def object_key_from_url(self, url: str) -> str | None:
        return self._url_to_key.get(url)

    async def stat(self, *, object_key: str) -> ObjectMetadata | None:
        return self._meta.get(object_key)


class _Monotonic:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


async def _identity(_character_id: str) -> tuple[str, str] | None:
    return ("t1", "a1")


async def _no_identity(_character_id: str) -> tuple[str, str] | None:
    return None


def _adapter(
    *,
    client,
    storage=None,
    identity=_identity,
    ledger=None,
    monotonic=None,
):
    return HostedChannelProactiveDeliveryAdapter(
        client=client,
        object_storage=storage or _FakeStorage(),
        identity_resolver=identity,
        event_ledger=ledger,
        monotonic=monotonic or _Monotonic(),
    )


def _envelope(
    *,
    attachments: tuple[OutboundAttachment, ...] = (),
    event_id: str = "evt-1",
) -> ProactiveEnvelope:
    return ProactiveEnvelope(
        event_id=event_id,
        tenant_id="t1",
        account_id="a1",
        character_id="c1",
        kind=ENVELOPE_KIND_PROACTIVE,
        segments=(ProactiveSegment(text="今天好累"),),
        attachments=attachments,
        locale="zh-TW",
        created_at=_NOW,
        expires_at=_NOW.replace(hour=10),
    )


# ----------------------------- accept ---------------------------------- #

@pytest.mark.asyncio
async def test_accept_accepted_maps_and_sends_wire_payload() -> None:
    client = _FakeClient(
        accept=ChannelAcceptResult(status=ACCEPT_ACCEPTED, delivery_id="d-7"),
    )
    adapter = _adapter(client=client)
    acceptance = await adapter.accept(_envelope())
    assert acceptance.status is DeliveryAcceptanceStatus.ACCEPTED
    assert acceptance.delivery_id == "d-7"
    payload = client.accept_payloads[0]
    assert payload["event_id"] == "evt-1"
    assert payload["segments"] == [{"text": "今天好累"}]
    assert payload["attachments"] == []
    assert payload["created_at"] == "2026-07-24T09:00:00+00:00"
    assert payload["contract_version"] == 1


@pytest.mark.asyncio
async def test_accept_no_endpoint_is_terminal() -> None:
    client = _FakeClient(accept=ChannelAcceptResult(status=ACCEPT_NO_ENDPOINT))
    acceptance = await _adapter(client=client).accept(_envelope())
    assert acceptance.status is DeliveryAcceptanceStatus.TERMINAL
    assert acceptance.reason == "no_endpoint"


@pytest.mark.asyncio
async def test_accept_conflict_is_terminal() -> None:
    client = _FakeClient(accept=ChannelAcceptResult(status=ACCEPT_CONFLICT))
    acceptance = await _adapter(client=client).accept(_envelope())
    assert acceptance.status is DeliveryAcceptanceStatus.TERMINAL
    assert acceptance.reason == "conflict"


@pytest.mark.asyncio
async def test_accept_transient_propagates() -> None:
    client = _FakeClient(accept=ChannelDeliveryTransientError("503"))
    with pytest.raises(ChannelDeliveryTransientError):
        await _adapter(client=client).accept(_envelope())


@pytest.mark.asyncio
async def test_accept_maps_url_attachments_and_skips_unresolvable() -> None:
    storage = _FakeStorage()
    storage.add(
        url="/v1/public/img-a",
        key="img-a",
        content_type="image/png",
        size_bytes=1234,
        sha256="a" * 64,
    )
    client = _FakeClient(
        accept=ChannelAcceptResult(status=ACCEPT_ACCEPTED, delivery_id="d"),
    )
    adapter = _adapter(client=client, storage=storage)
    envelope = _envelope(
        attachments=(
            OutboundAttachment(
                kind="image", url="/v1/public/img-a", mime_type="image/png",
            ),
            OutboundAttachment(
                kind="image", url="https://external.example/unknown.png",
            ),
        ),
    )
    await adapter.accept(envelope)
    refs = client.accept_payloads[0]["attachments"]
    assert refs == [
        {
            "object_ref": "img-a",
            "media_type": "image/png",
            "size_bytes": 1234,
            "sha256": "a" * 64,
        },
    ]


@pytest.mark.asyncio
async def test_accept_defensive_ledger_missing_row_still_sends() -> None:
    ledger = InMemoryExternalProactiveEventRepository()  # empty: no pre-send row
    client = _FakeClient(
        accept=ChannelAcceptResult(status=ACCEPT_ACCEPTED, delivery_id="d"),
    )
    adapter = _adapter(client=client, ledger=ledger)
    acceptance = await adapter.accept(_envelope())
    assert acceptance.status is DeliveryAcceptanceStatus.ACCEPTED
    assert len(client.accept_payloads) == 1


# --------------------------- eligibility ------------------------------- #

@pytest.mark.asyncio
async def test_eligibility_positive_cached_for_60s() -> None:
    client = _FakeClient(eligibility=True)
    clock = _Monotonic()
    adapter = _adapter(client=client, monotonic=clock)
    first = await adapter.check_eligibility("c1")
    assert first.eligible is True
    assert first.ttl_seconds == 60.0
    clock.t += 59.0
    second = await adapter.check_eligibility("c1")
    assert second.eligible is True
    assert client.eligibility_calls == 1  # served from cache


@pytest.mark.asyncio
async def test_eligibility_negative_cached_for_30s() -> None:
    client = _FakeClient(eligibility=False)
    clock = _Monotonic()
    adapter = _adapter(client=client, monotonic=clock)
    first = await adapter.check_eligibility("c1")
    assert first.eligible is False
    assert first.ttl_seconds == 30.0
    clock.t += 29.0
    second = await adapter.check_eligibility("c1")
    assert client.eligibility_calls == 1
    # LQ-F2 fallback state: the channel declined with no body ``reason`` (the
    # fake client's bare-bool convenience), so the pre-LQ-F2 fallback text
    # must survive verbatim — an absent body field cannot silently change
    # what an existing consumer (the LINE reactivation candidate listing)
    # reads, and the cached re-read must agree with the live one.
    assert first.reason == "no active channel endpoint"
    assert second.reason == "no active channel endpoint"


@pytest.mark.asyncio
async def test_eligibility_reason_passes_through_from_channel_body() -> None:
    """LQ-F2: the channel's own decline reason reaches the caller verbatim.

    Regression lock for the bug this ticket fixes — before LQ-F2 the adapter
    discarded the channel body's ``reason`` and hardcoded "no active channel
    endpoint" for every decline, so the LINE reactivation candidate listing
    (which renders ``verdict.reason`` straight to an operator) could not tell
    a quota-exhausted character apart from a genuinely broken binding.
    """
    client = _FakeClient(
        eligibility=EligibilityVerdict(
            eligible=False, reason="quota_character_monthly_exhausted",
        ),
    )
    clock = _Monotonic()
    adapter = _adapter(client=client, monotonic=clock)
    first = await adapter.check_eligibility("c1")
    assert first.eligible is False
    assert first.reason == "quota_character_monthly_exhausted"
    # The cached re-read must carry the SAME specific reason, not silently
    # regress to the generic fallback text.
    clock.t += 1.0
    second = await adapter.check_eligibility("c1")
    assert client.eligibility_calls == 1  # served from cache
    assert second.reason == "quota_character_monthly_exhausted"


@pytest.mark.asyncio
async def test_eligibility_cache_expires_and_requeries() -> None:
    client = _FakeClient(eligibility=True)
    clock = _Monotonic()
    adapter = _adapter(client=client, monotonic=clock)
    await adapter.check_eligibility("c1")
    clock.t += 61.0
    await adapter.check_eligibility("c1")
    assert client.eligibility_calls == 2


@pytest.mark.asyncio
async def test_eligibility_transient_is_negative_short_ttl() -> None:
    client = _FakeClient(eligibility=ChannelDeliveryTransientError("500"))
    result = await _adapter(client=client).check_eligibility("c1")
    assert result.eligible is False
    assert result.ttl_seconds == 30.0
    # A transport failure is not a channel-supplied decline reason — the
    # reason text must stay this transient-specific string, never a body
    # ``reason`` value (there was no body) and never the no-endpoint fallback.
    assert result.reason == "channel eligibility unavailable"


@pytest.mark.asyncio
async def test_eligibility_no_identity_not_eligible_no_call() -> None:
    client = _FakeClient(eligibility=True)
    result = await _adapter(
        client=client, identity=_no_identity,
    ).check_eligibility("c1")
    assert result.eligible is False
    assert client.eligibility_calls == 0
