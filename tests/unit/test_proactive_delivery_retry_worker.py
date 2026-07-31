"""ProactiveDeliveryRetryWorker lock (LH4, DR-LH0-005).

A still-``pending`` delivery event is re-sent through the same port with the SAME
``event_id`` (never re-decided); a transient failure leaves it pending; a terminal
rejection closes it; an expired row is retired without a send. The real in-memory
ledger drives the accepted/pending state transitions end to end.
"""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.application.services.proactive_delivery.retry_worker import (
    ProactiveDeliveryRetryWorker,
)
from kokoro_link.contracts.external_proactive import (
    ENVELOPE_KIND_PROACTIVE,
    DeliveryAcceptance,
    DeliveryAcceptanceStatus,
    ExternalProactiveConfigurationError,
    ProactiveEnvelope,
    ProactiveSegment,
    envelope_to_payload,
)
from kokoro_link.contracts.external_proactive_ledger import (
    ExternalProactiveEvent,
    ExternalProactiveEventState,
)
from kokoro_link.infrastructure.cloud.hosted_channel_proactive_client import (
    ChannelDeliveryTransientError,
)
from kokoro_link.infrastructure.repositories.in_memory_external_proactive_events import (  # noqa: E501
    InMemoryExternalProactiveEventRepository,
)

_NOW = datetime(2026, 7, 24, 9, 0, tzinfo=timezone.utc)


class _FakeDelivery:
    def __init__(self, *, result=None, raise_exc=None) -> None:
        self._result = result
        self._raise = raise_exc
        self.envelopes: list[ProactiveEnvelope] = []

    async def check_eligibility(self, character_id):  # pragma: no cover
        raise NotImplementedError

    async def accept(self, envelope: ProactiveEnvelope) -> DeliveryAcceptance:
        self.envelopes.append(envelope)
        if self._raise is not None:
            raise self._raise
        return self._result


class _RecordingLedger:
    """Returns a controlled due list and records the mark_* transitions.

    Used for the expired branch, which the real in-memory ledger filters out of
    ``list_pending_due`` (so it can never surface there)."""

    def __init__(self, due: list[ExternalProactiveEvent]) -> None:
        self._due = due
        self.accepted: list[str] = []
        self.terminal: list[tuple[str, str]] = []
        self.expired: list[str] = []

    async def list_pending_due(self, *, now, limit=100):
        return list(self._due)

    async def mark_accepted(self, event_id, *, now) -> bool:
        self.accepted.append(event_id)
        return True

    async def mark_terminal(self, event_id, *, reason, now) -> bool:
        self.terminal.append((event_id, reason))
        return True

    async def mark_expired(self, event_id, *, now) -> bool:
        self.expired.append(event_id)
        return True

    async def get(self, event_id):  # pragma: no cover
        return None

    async def save_pre_send(self, **kwargs):  # pragma: no cover
        raise NotImplementedError


def _envelope(event_id: str = "evt-1") -> ProactiveEnvelope:
    return ProactiveEnvelope(
        event_id=event_id,
        tenant_id="t1",
        account_id="a1",
        character_id="c1",
        kind=ENVELOPE_KIND_PROACTIVE,
        segments=(ProactiveSegment(text="hi"),),
        attachments=(),
        locale="zh-TW",
        created_at=_NOW,
        expires_at=_NOW + timedelta(hours=1),
    )


def _event(
    *, event_id: str, expires_at: datetime,
) -> ExternalProactiveEvent:
    envelope = _envelope(event_id)
    return ExternalProactiveEvent(
        event_id=event_id,
        tenant_id="t1",
        account_id="a1",
        character_id="c1",
        kind=ENVELOPE_KIND_PROACTIVE,
        envelope_json=json.dumps(
            envelope_to_payload(envelope), ensure_ascii=False, sort_keys=True,
        ),
        payload_hash="unused",
        state=ExternalProactiveEventState.PENDING,
        accept_attempts=0,
        last_error=None,
        created_at=_NOW,
        updated_at=_NOW,
        expires_at=expires_at,
    )


async def _seed_pending(
    ledger: InMemoryExternalProactiveEventRepository, envelope: ProactiveEnvelope,
) -> None:
    await ledger.save_pre_send(
        event_id=envelope.event_id,
        tenant_id=envelope.tenant_id,
        account_id=envelope.account_id,
        character_id=envelope.character_id,
        kind=envelope.kind,
        envelope_json=json.dumps(
            envelope_to_payload(envelope), ensure_ascii=False, sort_keys=True,
        ),
        payload_hash="h",
        expires_at=envelope.expires_at,
        now=_NOW,
    )


@pytest.mark.asyncio
async def test_pending_resends_same_event_and_marks_accepted() -> None:
    ledger = InMemoryExternalProactiveEventRepository()
    envelope = _envelope("evt-1")
    await _seed_pending(ledger, envelope)
    delivery = _FakeDelivery(
        result=DeliveryAcceptance(
            status=DeliveryAcceptanceStatus.ACCEPTED, delivery_id="d",
        ),
    )
    worker = ProactiveDeliveryRetryWorker(
        ledger=ledger, external_delivery=delivery,
    )
    await worker.tick(now=_NOW + timedelta(minutes=1))

    assert [e.event_id for e in delivery.envelopes] == ["evt-1"]
    stored = await ledger.get("evt-1")
    assert stored.state is ExternalProactiveEventState.ACCEPTED


@pytest.mark.asyncio
async def test_transient_leaves_row_pending() -> None:
    ledger = InMemoryExternalProactiveEventRepository()
    envelope = _envelope("evt-2")
    await _seed_pending(ledger, envelope)
    delivery = _FakeDelivery(
        raise_exc=ChannelDeliveryTransientError("channel 503"),
    )
    worker = ProactiveDeliveryRetryWorker(
        ledger=ledger, external_delivery=delivery,
    )
    await worker.tick(now=_NOW + timedelta(minutes=1))

    stored = await ledger.get("evt-2")
    assert stored.state is ExternalProactiveEventState.PENDING


@pytest.mark.asyncio
async def test_unexpected_delivery_failure_is_visible_and_terminal(
    caplog: pytest.LogCaptureFixture,
) -> None:
    ledger = InMemoryExternalProactiveEventRepository()
    envelope = _envelope("evt-unexpected")
    await _seed_pending(ledger, envelope)
    worker = ProactiveDeliveryRetryWorker(
        ledger=ledger,
        external_delivery=_FakeDelivery(raise_exc=RuntimeError("bad wiring")),
    )

    with caplog.at_level(logging.ERROR):
        await worker.tick(now=_NOW + timedelta(minutes=1))

    stored = await ledger.get("evt-unexpected")
    assert stored.state is ExternalProactiveEventState.TERMINAL
    assert stored.last_error == "unexpected_delivery_error:RuntimeError"
    assert "unexpected delivery failure event=evt-unexpected" in caplog.text


@pytest.mark.asyncio
async def test_configuration_failure_is_visible_and_terminal(
    caplog: pytest.LogCaptureFixture,
) -> None:
    ledger = InMemoryExternalProactiveEventRepository()
    envelope = _envelope("evt-config")
    await _seed_pending(ledger, envelope)
    worker = ProactiveDeliveryRetryWorker(
        ledger=ledger,
        external_delivery=_FakeDelivery(
            raise_exc=ExternalProactiveConfigurationError("missing base URL"),
        ),
    )

    with caplog.at_level(logging.ERROR):
        await worker.tick(now=_NOW + timedelta(minutes=1))

    stored = await ledger.get("evt-config")
    assert stored.state is ExternalProactiveEventState.TERMINAL
    assert stored.last_error == (
        "delivery_configuration_error:ExternalProactiveConfigurationError"
    )
    assert "configuration failure event=evt-config" in caplog.text


@pytest.mark.asyncio
async def test_malformed_acceptance_is_visible_and_terminal(
    caplog: pytest.LogCaptureFixture,
) -> None:
    ledger = InMemoryExternalProactiveEventRepository()
    envelope = _envelope("evt-malformed-result")
    await _seed_pending(ledger, envelope)
    worker = ProactiveDeliveryRetryWorker(
        ledger=ledger,
        external_delivery=_FakeDelivery(result=None),
    )

    with caplog.at_level(logging.ERROR):
        await worker.tick(now=_NOW + timedelta(minutes=1))

    stored = await ledger.get("evt-malformed-result")
    assert stored.state is ExternalProactiveEventState.TERMINAL
    assert stored.last_error == "malformed_delivery_acceptance:NoneType"
    assert "malformed acceptance event=evt-malformed-result" in caplog.text


@pytest.mark.asyncio
async def test_malformed_envelope_is_visible_and_terminal(
    caplog: pytest.LogCaptureFixture,
) -> None:
    event = replace(
        _event(event_id="evt-malformed", expires_at=_NOW + timedelta(hours=1)),
        envelope_json='{not valid json',
    )
    ledger = _RecordingLedger([event])
    delivery = _FakeDelivery(
        result=DeliveryAcceptance(status=DeliveryAcceptanceStatus.ACCEPTED),
    )
    worker = ProactiveDeliveryRetryWorker(
        ledger=ledger,
        external_delivery=delivery,
    )

    with caplog.at_level(logging.ERROR):
        await worker.tick(now=_NOW)

    assert ledger.terminal == [("evt-malformed", "malformed_envelope")]
    assert delivery.envelopes == []
    assert "malformed envelope event=evt-malformed" in caplog.text


@pytest.mark.asyncio
async def test_terminal_rejection_marks_terminal() -> None:
    ledger = InMemoryExternalProactiveEventRepository()
    envelope = _envelope("evt-3")
    await _seed_pending(ledger, envelope)
    delivery = _FakeDelivery(
        result=DeliveryAcceptance(
            status=DeliveryAcceptanceStatus.TERMINAL, reason="no_endpoint",
        ),
    )
    worker = ProactiveDeliveryRetryWorker(
        ledger=ledger, external_delivery=delivery,
    )
    await worker.tick(now=_NOW + timedelta(minutes=1))

    stored = await ledger.get("evt-3")
    assert stored.state is ExternalProactiveEventState.TERMINAL
    assert stored.last_error == "no_endpoint"


@pytest.mark.asyncio
async def test_expired_row_is_marked_expired_not_sent() -> None:
    expired = _event(
        event_id="evt-old", expires_at=_NOW - timedelta(minutes=5),
    )
    ledger = _RecordingLedger([expired])
    delivery = _FakeDelivery(
        result=DeliveryAcceptance(status=DeliveryAcceptanceStatus.ACCEPTED),
    )
    worker = ProactiveDeliveryRetryWorker(
        ledger=ledger, external_delivery=delivery,
    )
    await worker.tick(now=_NOW)

    assert ledger.expired == ["evt-old"]
    assert delivery.envelopes == []  # never re-sent
    assert ledger.accepted == []
