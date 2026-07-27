"""sol review-fix locks: H4 (dual local/hosted adapters never cross identities),
L12 (gate-resolved target snapshot pins the send), M8 (accepted-but-unrecorded
line history is re-appended by the retry worker)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.application.services.proactive_delivery.line_conversation_recorder import (  # noqa: E501
    HostedLineConversationRecorder,
)
from kokoro_link.application.services.proactive_delivery.local_adapter import (
    LocalDeliveryTarget,
    LocalMessagingProactiveDeliveryAdapter,
)
from kokoro_link.application.services.proactive_delivery.retry_worker import (
    ProactiveDeliveryRetryWorker,
)
from kokoro_link.application.services.proactive_dispatcher import ProactiveDispatcher
from kokoro_link.contracts.external_proactive import (
    DeliveryAcceptance,
    DeliveryAcceptanceStatus,
    DeliveryEligibility,
    ENVELOPE_KIND_PROACTIVE,
    ProactiveEnvelope,
    ProactiveSegment,
    envelope_to_payload,
)
from kokoro_link.contracts.proactive import (
    ProactiveContext,
    ProactiveDecision,
    ProactiveDeciderPort,
)
from kokoro_link.domain.entities.channel_binding import ChannelBinding
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.platform import Platform
from kokoro_link.domain.value_objects.proactive_outcome import ProactiveOutcome
from kokoro_link.domain.value_objects.proactive_trigger import ProactiveTrigger
from kokoro_link.infrastructure.proactive.heuristic_gate import HeuristicProactiveGate
from kokoro_link.infrastructure.repositories.in_memory_conversations import (
    InMemoryConversationRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_external_proactive_events import (  # noqa: E501
    InMemoryExternalProactiveEventRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_proactive_attempts import (
    InMemoryProactiveAttemptRepository,
)
from tests.unit._messaging_harness import (
    build_messaging_harness,
    create_character,
    create_telegram_account,
)

pytestmark = pytest.mark.asyncio

_NOW = datetime(2026, 7, 24, 9, 0, tzinfo=timezone.utc)
_HOSTED_IDENTITY = ("tenant-7", "account-9")


# --- shared fakes -----------------------------------------------------------


class _SpyDecider(ProactiveDeciderPort):
    def __init__(self, message: str = "在忙嗎？") -> None:
        self._decision = ProactiveDecision(True, "ok", message)
        self.calls = 0

    async def decide(self, context: ProactiveContext) -> ProactiveDecision:
        self.calls += 1
        return self._decision


class _RecordingPort:
    def __init__(self, *, eligible: bool = True) -> None:
        self._eligible = eligible
        self.accepted: list[tuple[ProactiveEnvelope, object]] = []

    async def check_eligibility(self, character_id: str) -> DeliveryEligibility:
        return DeliveryEligibility(eligible=self._eligible)

    async def accept(self, envelope, *, target=None) -> DeliveryAcceptance:
        self.accepted.append((envelope, target))
        return DeliveryAcceptance(
            status=DeliveryAcceptanceStatus.ACCEPTED, delivery_id="del-1",
        )


async def _identity(_character_id: str):
    return _HOSTED_IDENTITY


def _dispatcher(harness, *, local_port, hosted_port, decider):
    return ProactiveDispatcher(
        character_repository=harness.character_repository,
        conversation_repository=harness.conversation_repository,
        account_repository=harness.account_repository,
        binding_repository=harness.binding_repository,
        attempt_repository=InMemoryProactiveAttemptRepository(),
        gate=HeuristicProactiveGate(
            local_tz=timezone.utc, quiet_hour_start=0, quiet_hour_end=0,
        ),
        decider=decider,
        adapters={
            Platform.TELEGRAM: harness.telegram_adapter,
            Platform.LINE: harness.line_adapter,
        },
        external_delivery=hosted_port,
        local_delivery=local_port,
        hosted_delivery=hosted_port,
        hosted_identity_resolver=_identity,
    )


async def _proactive_character(harness, *, with_binding: bool):
    character = await create_character(harness)
    entity = await harness.character_repository.get(character.id)
    await harness.character_repository.save(
        entity.update(
            name=None, summary=None, personality=None, interests=None,
            speaking_style=None, boundaries=None, aspirations=None,
            appearance=None,
            state=CharacterState(
                emotion="calm", affection=50, fatigue=0, trust=50,
                energy=100,
                # Anchor to the fixed _NOW the tests evaluate at — wall-clock here
                # made idle_minutes go negative once real time drifted past _NOW,
                # flipping the heuristic gate to "user active" (time-of-day flake).
                last_active_at=_NOW - timedelta(minutes=60),
            ),
            proactive_enabled=True,
            accepts_web_proactive=False,
        ),
    )
    if with_binding:
        account = await create_telegram_account(harness, character_id=character.id)
        await harness.binding_repository.save(
            ChannelBinding.create(
                account_id=account.id, chat_ref="c1", accepts_proactive=True,
            ),
        )
    return character


# --- H4: local binding -> local adapter; hosted target -> hosted adapter -----


async def test_h4_local_binding_routes_to_local_adapter() -> None:
    harness = build_messaging_harness()
    character = await _proactive_character(harness, with_binding=True)
    local_port, hosted_port = _RecordingPort(), _RecordingPort()
    dispatcher = _dispatcher(
        harness, local_port=local_port, hosted_port=hosted_port,
        decider=_SpyDecider(),
    )

    attempt = await dispatcher.evaluate(
        character_id=character.id, trigger=ProactiveTrigger.TICK,
    )

    assert attempt.outcome == ProactiveOutcome.SENT
    # The local binding path used the LOCAL adapter with a target snapshot;
    # the hosted adapter was never handed the local identity.
    assert len(local_port.accepted) == 1
    assert isinstance(local_port.accepted[0][1], LocalDeliveryTarget)
    assert hosted_port.accepted == []


async def test_h4_local_binding_event_never_enters_the_hosted_ledger() -> None:
    """R-H4: in hosted mode the local-binding path must NOT write the shared
    pre-send ledger — otherwise the hosted retry worker (which only ever re-sends
    through the HOSTED adapter) would deliver a LOCAL event to the Cloud Channel.
    The local send is synchronous with no lost-response window, so it needs no
    ledger row."""
    harness = build_messaging_harness()
    character = await _proactive_character(harness, with_binding=True)
    ledger = InMemoryExternalProactiveEventRepository()
    local_port, hosted_port = _RecordingPort(), _RecordingPort()
    dispatcher = ProactiveDispatcher(
        character_repository=harness.character_repository,
        conversation_repository=harness.conversation_repository,
        account_repository=harness.account_repository,
        binding_repository=harness.binding_repository,
        attempt_repository=InMemoryProactiveAttemptRepository(),
        gate=HeuristicProactiveGate(
            local_tz=timezone.utc, quiet_hour_start=0, quiet_hour_end=0,
        ),
        decider=_SpyDecider(),
        adapters={
            Platform.TELEGRAM: harness.telegram_adapter,
            Platform.LINE: harness.line_adapter,
        },
        external_delivery=hosted_port,
        local_delivery=local_port,
        hosted_delivery=hosted_port,
        hosted_identity_resolver=_identity,
        proactive_event_ledger=ledger,
    )

    attempt = await dispatcher.evaluate(
        character_id=character.id, trigger=ProactiveTrigger.TICK, now=_NOW,
    )

    assert attempt.outcome == ProactiveOutcome.SENT
    assert len(local_port.accepted) == 1  # local binding used the LOCAL adapter
    # The local event was NOT recorded in the shared ledger...
    due = await ledger.list_pending_due(now=_NOW, limit=10)
    assert due == []
    unrecorded = await ledger.list_accepted_unrecorded(now=_NOW, limit=10)
    assert unrecorded == []

    # ...so the hosted retry worker has nothing to re-send through the hosted
    # adapter — a local event is never misrouted to the Cloud Channel.
    worker = ProactiveDeliveryRetryWorker(
        ledger=ledger, external_delivery=hosted_port,
    )
    await worker.tick(now=_NOW)
    assert hosted_port.accepted == []


async def test_h4_no_binding_routes_to_hosted_adapter() -> None:
    harness = build_messaging_harness()
    character = await _proactive_character(harness, with_binding=False)
    local_port, hosted_port = _RecordingPort(), _RecordingPort()
    dispatcher = _dispatcher(
        harness, local_port=local_port, hosted_port=hosted_port,
        decider=_SpyDecider(),
    )

    attempt = await dispatcher.evaluate(
        character_id=character.id, trigger=ProactiveTrigger.TICK,
    )

    assert attempt.outcome == ProactiveOutcome.SENT
    # No local binding → hosted channel; the local adapter is never touched.
    assert len(hosted_port.accepted) == 1
    envelope, _target = hosted_port.accepted[0]
    assert (envelope.tenant_id, envelope.account_id) == _HOSTED_IDENTITY
    assert local_port.accepted == []


# --- L12: the gate-resolved target snapshot is sent even if the binding moves


async def test_l12_target_snapshot_is_sent_even_after_binding_changes() -> None:
    harness = build_messaging_harness()
    character = await create_character(harness)
    account = await create_telegram_account(harness, character_id=character.id)
    # The gate resolved THIS binding. It is deliberately NOT in the repository,
    # so a re-resolution at delivery time would find nothing (or something
    # else) — the send must still go to the gate-resolved target.
    original = ChannelBinding.create(
        account_id=account.id, chat_ref="original-chat", accepts_proactive=True,
    )
    adapter = LocalMessagingProactiveDeliveryAdapter(
        account_repository=harness.account_repository,
        binding_repository=harness.binding_repository,
        conversation_repository=harness.conversation_repository,
        adapters={Platform.TELEGRAM: harness.telegram_adapter},
    )
    envelope = ProactiveEnvelope(
        event_id="evt-1", tenant_id="default", account_id="acct",
        character_id=character.id, kind=ENVELOPE_KIND_PROACTIVE,
        segments=(ProactiveSegment(text="hi"),), attachments=(),
        locale="zh-TW", created_at=_NOW, expires_at=_NOW + timedelta(hours=1),
    )

    await adapter.accept(
        envelope,
        target=LocalDeliveryTarget(binding=original, account=account),
    )

    assert len(harness.telegram_adapter.sent) == 1
    assert harness.telegram_adapter.sent[0].chat_ref == "original-chat"


# --- M8: retry worker back-fills the accepted-but-unrecorded line history ----


def _accepted_envelope() -> ProactiveEnvelope:
    return ProactiveEnvelope(
        event_id="evt-m8", tenant_id="t", account_id="a",
        character_id="char-m8", kind=ENVELOPE_KIND_PROACTIVE,
        segments=(ProactiveSegment(text="補寫的訊息"),), attachments=(),
        locale="zh-TW", created_at=_NOW, expires_at=_NOW + timedelta(hours=1),
    )


class _NoopDelivery:
    async def check_eligibility(self, character_id: str) -> DeliveryEligibility:
        return DeliveryEligibility(eligible=True)

    async def accept(self, envelope, *, target=None) -> DeliveryAcceptance:
        return DeliveryAcceptance(status=DeliveryAcceptanceStatus.ACCEPTED)


async def test_m8_retry_worker_records_accepted_unrecorded_conversation() -> None:
    ledger = InMemoryExternalProactiveEventRepository()
    envelope = _accepted_envelope()
    import json
    await ledger.save_pre_send(
        event_id=envelope.event_id, tenant_id=envelope.tenant_id,
        account_id=envelope.account_id, character_id=envelope.character_id,
        kind=envelope.kind, envelope_json=json.dumps(envelope_to_payload(envelope)),
        payload_hash="h", expires_at=envelope.expires_at, now=_NOW,
    )
    # Delivered (accepted) but the conversation append was lost.
    assert await ledger.mark_accepted(envelope.event_id, now=_NOW) is True

    conversations = InMemoryConversationRepository()
    recorder = HostedLineConversationRecorder(
        conversation_repository=conversations,
    )
    worker = ProactiveDeliveryRetryWorker(
        ledger=ledger,
        external_delivery=_NoopDelivery(),
        conversation_recorder=recorder,
    )

    await worker.tick(now=_NOW + timedelta(seconds=5))

    # The line history was back-filled and the ledger flag flipped.
    conversation = await conversations.latest_for_character(
        "char-m8", source="line",
    )
    assert conversation is not None
    assert [m.content for m in conversation.messages] == ["補寫的訊息"]
    stored = await ledger.get(envelope.event_id)
    assert stored.conversation_recorded is True

    # Idempotent: a second sweep neither doubles the row nor re-flips anything.
    await worker.tick(now=_NOW + timedelta(seconds=10))
    conversation = await conversations.latest_for_character(
        "char-m8", source="line",
    )
    assert len(conversation.messages) == 1
