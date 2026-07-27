"""Pre-send ledger discipline on the dispatcher's HOSTED sink (LH4, DR-LH0-005).

The dispatcher must durably record the immutable envelope BEFORE calling the
hosted Cloud-Channel delivery port, so a lost / crashed response leaves a
``pending`` row that can be re-sent from the ledger without re-running the judge
/ decider. The ledger lives on the hosted path ONLY: the local-binding path
delivers synchronously with no lost-response window, so it never writes the
ledger (R-H4 — otherwise the hosted retry worker would re-send a local event to
the Cloud Channel). See ``test_sol_review_proactive_delivery`` for the local path
NOT writing the ledger.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.application.services.proactive_dispatcher import ProactiveDispatcher
from kokoro_link.contracts.external_proactive import (
    DeliveryAcceptance,
    DeliveryAcceptanceStatus,
    DeliveryEligibility,
    ProactiveEnvelope,
)
from kokoro_link.contracts.external_proactive_ledger import (
    ExternalProactiveEventState,
)
from kokoro_link.contracts.proactive import (
    ProactiveContext,
    ProactiveDecision,
    ProactiveDeciderPort,
)
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.platform import Platform
from kokoro_link.domain.value_objects.proactive_outcome import ProactiveOutcome
from kokoro_link.domain.value_objects.proactive_trigger import ProactiveTrigger
from kokoro_link.infrastructure.proactive.heuristic_gate import HeuristicProactiveGate
from kokoro_link.infrastructure.repositories.in_memory_external_proactive_events import (
    InMemoryExternalProactiveEventRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_proactive_attempts import (
    InMemoryProactiveAttemptRepository,
)
from tests.unit._messaging_harness import (
    build_messaging_harness,
    create_character,
)


class _FixedDecider(ProactiveDeciderPort):
    def __init__(self, decision: ProactiveDecision) -> None:
        self._decision = decision

    async def decide(self, context: ProactiveContext) -> ProactiveDecision:
        return self._decision


class _RecordingPort:
    """Fake external delivery port that snapshots the ledger state observed at
    ``accept`` time — proving the pre-send record was already durable."""

    def __init__(self, ledger, *, crash: bool = False) -> None:
        self._ledger = ledger
        self._crash = crash
        self.accepted_event_ids: list[str] = []
        self.ledger_state_at_accept: list[str | None] = []

    async def check_eligibility(self, character_id: str) -> DeliveryEligibility:
        return DeliveryEligibility(eligible=True)

    async def accept(
        self, envelope: ProactiveEnvelope, *, target: object | None = None,
    ) -> DeliveryAcceptance:
        stored = await self._ledger.get(envelope.event_id)
        self.ledger_state_at_accept.append(stored.state if stored else None)
        self.accepted_event_ids.append(envelope.event_id)
        if self._crash:
            raise RuntimeError("channel unreachable — response lost")
        return DeliveryAcceptance(
            status=DeliveryAcceptanceStatus.ACCEPTED, delivery_id="chan-1",
        )


_HOSTED_IDENTITY = ("tenant-7", "account-9")


async def _hosted_identity(_character_id: str):
    return _HOSTED_IDENTITY


async def _enable_character_no_binding(harness):
    """A proactive character with NO local binding → the tick routes to the
    hosted Cloud-Channel path (the one that writes the pre-send ledger)."""
    character = await create_character(harness)
    entity = await harness.character_repository.get(character.id)
    assert entity is not None
    await harness.character_repository.save(
        entity.update(
            name=None, summary=None, personality=None, interests=None,
            speaking_style=None, boundaries=None, aspirations=None,
            appearance=None,
            state=CharacterState(
                emotion="neutral", affection=50, fatigue=0, trust=50,
                energy=100,
                last_active_at=datetime.now(timezone.utc) - timedelta(minutes=60),
            ),
            proactive_enabled=True,
            # The hosted channel is the only sink — a crashed send must error
            # the tick (no web fan-out masking it).
            accepts_web_proactive=False,
        ),
    )
    return character


def _dispatcher(harness, *, ledger, port):
    return ProactiveDispatcher(
        character_repository=harness.character_repository,
        conversation_repository=harness.conversation_repository,
        account_repository=harness.account_repository,
        binding_repository=harness.binding_repository,
        attempt_repository=InMemoryProactiveAttemptRepository(),
        gate=HeuristicProactiveGate(
            local_tz=timezone.utc, quiet_hour_start=0, quiet_hour_end=0,
        ),
        decider=_FixedDecider(ProactiveDecision(True, "ok", "嗨")),
        adapters={
            Platform.TELEGRAM: harness.telegram_adapter,
            Platform.LINE: harness.line_adapter,
        },
        external_delivery=port,
        hosted_delivery=port,
        hosted_identity_resolver=_hosted_identity,
        proactive_event_ledger=ledger,
    )


@pytest.mark.asyncio
async def test_pre_send_is_durable_before_accept_then_marked_accepted() -> None:
    harness = build_messaging_harness()
    character = await _enable_character_no_binding(harness)
    ledger = InMemoryExternalProactiveEventRepository()
    port = _RecordingPort(ledger)
    dispatcher = _dispatcher(harness, ledger=ledger, port=port)

    attempt = await dispatcher.evaluate(
        character_id=character.id, trigger=ProactiveTrigger.TICK,
    )

    assert attempt.outcome == ProactiveOutcome.SENT
    # Save happened BEFORE accept: the ledger already held a pending row when
    # the port's accept ran.
    assert port.ledger_state_at_accept == [ExternalProactiveEventState.PENDING]
    # The channel's delivery_id flowed onto the audit row.
    assert attempt.binding_id == "chan-1"
    # Successful acceptance closed the ledger row out.
    event_id = port.accepted_event_ids[0]
    stored = await ledger.get(event_id)
    assert stored is not None
    assert stored.state is ExternalProactiveEventState.ACCEPTED
    assert stored.accept_attempts == 1


@pytest.mark.asyncio
async def test_accept_crash_leaves_event_pending_for_retry() -> None:
    harness = build_messaging_harness()
    character = await _enable_character_no_binding(harness)
    ledger = InMemoryExternalProactiveEventRepository()
    port = _RecordingPort(ledger, crash=True)
    dispatcher = _dispatcher(harness, ledger=ledger, port=port)

    when = datetime.now(timezone.utc)
    attempt = await dispatcher.evaluate(
        character_id=character.id, trigger=ProactiveTrigger.TICK, now=when,
    )

    # Web is off + the only sink crashed → the tick errors, but the durable
    # event survives so it can be re-sent without re-running judge / decider.
    assert attempt.outcome == ProactiveOutcome.ERRORED
    assert port.ledger_state_at_accept == [ExternalProactiveEventState.PENDING]
    event_id = port.accepted_event_ids[0]
    stored = await ledger.get(event_id)
    assert stored is not None
    assert stored.state is ExternalProactiveEventState.PENDING
    # It shows up on the pending-due retry worklist.
    due = await ledger.list_pending_due(now=when, limit=10)
    assert [event.event_id for event in due] == [event_id]
