"""Hosted routing on the proactive dispatcher (LH4 Core-C).

Closes the two Core-B gaps: in hosted mode a character with NO local messaging
binding still routes its proactive push to the Cloud Channel, and the delivery
carries the character's real cloud ``(tenant_id, account_id)`` identity.

Locked here:

* eligible hosted identity → full chain (decider → pre-send ledger → accept →
  ``source="line"`` conversation append),
* channel-ineligible → the decider is never reached (cheap-gate order) and the
  tick logs NO_BINDING,
* no cloud mapping → the same NO_BINDING skip,
* line-conversation append conflict → one bounded re-read/retry, message
  recorded exactly once,
* ``deliver_pre_composed`` follows the same hosted dual-path,
* the real reverse identity resolver locks the cloud-mapped / unmapped states.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.application.services.proactive_delivery.hosted_identity import (
    build_hosted_delivery_identity_resolver,
)
from kokoro_link.application.services.proactive_dispatcher import ProactiveDispatcher
from kokoro_link.contracts.external_proactive import (
    DeliveryAcceptance,
    DeliveryAcceptanceStatus,
    DeliveryEligibility,
    ProactiveEnvelope,
)
from kokoro_link.contracts.proactive import (
    ProactiveContext,
    ProactiveDecision,
    ProactiveDeciderPort,
)
from kokoro_link.contracts.repositories import AppendResult
from kokoro_link.domain.entities.conversation import MessageRole
from kokoro_link.domain.entities.operator_profile import OperatorProfile
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.platform import Platform
from kokoro_link.domain.value_objects.proactive_outcome import ProactiveOutcome
from kokoro_link.domain.value_objects.proactive_trigger import ProactiveTrigger
from kokoro_link.infrastructure.proactive.heuristic_gate import HeuristicProactiveGate
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_external_proactive_events import (  # noqa: E501
    InMemoryExternalProactiveEventRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_operator_profile import (
    InMemoryOperatorProfileRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_proactive_attempts import (
    InMemoryProactiveAttemptRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_conversations import (
    InMemoryConversationRepository,
)
from tests.unit._messaging_harness import build_messaging_harness, create_character

_HOSTED_IDENTITY = ("tenant-7", "account-9")


class _SpyDecider(ProactiveDeciderPort):
    def __init__(self, decision: ProactiveDecision) -> None:
        self._decision = decision
        self.calls = 0

    async def decide(self, context: ProactiveContext) -> ProactiveDecision:
        self.calls += 1
        return self._decision


class _FakeHostedPort:
    """Hosted external-delivery port double: a cost preflight and an accept that
    snapshots the envelope it was handed."""

    def __init__(
        self,
        *,
        eligible: bool = True,
        status: DeliveryAcceptanceStatus = DeliveryAcceptanceStatus.ACCEPTED,
        delivery_id: str | None = "chan-del-1",
    ) -> None:
        self._eligible = eligible
        self._status = status
        self._delivery_id = delivery_id
        self.eligibility_calls = 0
        self.accepted: list[ProactiveEnvelope] = []

    async def check_eligibility(self, character_id: str) -> DeliveryEligibility:
        self.eligibility_calls += 1
        return DeliveryEligibility(eligible=self._eligible)

    async def accept(self, envelope: ProactiveEnvelope) -> DeliveryAcceptance:
        self.accepted.append(envelope)
        return DeliveryAcceptance(
            status=self._status, delivery_id=self._delivery_id,
        )


class _ConflictOnceConversations:
    """Wraps a real conversation repo but forces the FIRST ``append_messages``
    to lose the CAS race, so the dispatcher's bounded re-read/retry is exercised."""

    def __init__(self, inner: InMemoryConversationRepository) -> None:
        self._inner = inner
        self.append_calls = 0

    async def latest_for_character(self, character_id, *, source="web"):
        return await self._inner.latest_for_character(character_id, source=source)

    async def save(self, conversation, *, truncation=False):
        return await self._inner.save(conversation, truncation=truncation)

    async def append_messages(
        self, conversation_id, *, expected_next_position, messages,
        idempotency_key=None,
    ):
        self.append_calls += 1
        if self.append_calls == 1:
            return AppendResult(ok=False, conflict=True)
        return await self._inner.append_messages(
            conversation_id,
            expected_next_position=expected_next_position,
            messages=messages,
            idempotency_key=idempotency_key,
        )

    def __getattr__(self, name):  # delegate the rest verbatim
        return getattr(self._inner, name)


async def _identity(_character_id: str) -> "tuple[str, str] | None":
    return _HOSTED_IDENTITY


async def _no_identity(_character_id: str) -> "tuple[str, str] | None":
    return None


async def _enable_hosted_character(harness):
    """Proactive-enabled character with NO local binding and web off — the only
    possible sink is the hosted channel."""
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
            accepts_web_proactive=False,
        ),
    )
    return character


def _dispatcher(
    harness,
    *,
    port,
    decider,
    identity_resolver=_identity,
    ledger=None,
    conversation_repository=None,
):
    return ProactiveDispatcher(
        character_repository=harness.character_repository,
        conversation_repository=(
            conversation_repository or harness.conversation_repository
        ),
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
        external_delivery=port,
        proactive_event_ledger=ledger,
        hosted_identity_resolver=identity_resolver,
    )


@pytest.mark.asyncio
async def test_hosted_path_full_chain_records_line_conversation() -> None:
    harness = build_messaging_harness()
    character = await _enable_hosted_character(harness)
    ledger = InMemoryExternalProactiveEventRepository()
    port = _FakeHostedPort()
    decider = _SpyDecider(ProactiveDecision(True, "ok", "在忙嗎？"))
    dispatcher = _dispatcher(
        harness, port=port, decider=decider, ledger=ledger,
    )

    attempt = await dispatcher.evaluate(
        character_id=character.id, trigger=ProactiveTrigger.TICK,
    )

    assert attempt.outcome == ProactiveOutcome.SENT
    # Routed to hosted: the envelope carries the reverse-resolved cloud identity.
    assert len(port.accepted) == 1
    envelope = port.accepted[0]
    assert (envelope.tenant_id, envelope.account_id) == _HOSTED_IDENTITY
    assert envelope.character_id == character.id
    assert envelope.text == "在忙嗎？"
    # The channel's delivery id flows onto the audit row.
    assert attempt.binding_id == "chan-del-1"
    # Pre-send ledger closed out on acceptance.
    stored = await ledger.get(envelope.event_id)
    assert stored is not None
    # The proactive turn landed on the character's source="line" conversation.
    conversation = await harness.conversation_repository.latest_for_character(
        character.id, source="line",
    )
    assert conversation is not None
    assert len(conversation.messages) == 1
    assert conversation.messages[0].role is MessageRole.ASSISTANT
    assert conversation.messages[0].content == "在忙嗎？"


@pytest.mark.asyncio
async def test_hosted_ineligible_never_reaches_decider() -> None:
    harness = build_messaging_harness()
    character = await _enable_hosted_character(harness)
    port = _FakeHostedPort(eligible=False)
    decider = _SpyDecider(ProactiveDecision(True, "ok", "hi"))
    dispatcher = _dispatcher(harness, port=port, decider=decider)

    attempt = await dispatcher.evaluate(
        character_id=character.id, trigger=ProactiveTrigger.TICK,
    )

    assert attempt.outcome == ProactiveOutcome.NO_BINDING
    # Cheap-gate order: preflight was consulted, the decider never ran, nothing
    # was sent.
    assert port.eligibility_calls == 1
    assert decider.calls == 0
    assert port.accepted == []


@pytest.mark.asyncio
async def test_no_cloud_mapping_skips_like_no_binding() -> None:
    harness = build_messaging_harness()
    character = await _enable_hosted_character(harness)
    port = _FakeHostedPort()
    decider = _SpyDecider(ProactiveDecision(True, "ok", "hi"))
    dispatcher = _dispatcher(
        harness, port=port, decider=decider, identity_resolver=_no_identity,
    )

    attempt = await dispatcher.evaluate(
        character_id=character.id, trigger=ProactiveTrigger.TICK,
    )

    assert attempt.outcome == ProactiveOutcome.NO_BINDING
    # No cloud identity → we never even preflight the channel or run the decider.
    assert port.eligibility_calls == 0
    assert decider.calls == 0
    assert port.accepted == []


@pytest.mark.asyncio
async def test_line_conversation_append_conflict_retries_once() -> None:
    harness = build_messaging_harness()
    character = await _enable_hosted_character(harness)
    conversations = _ConflictOnceConversations(harness.conversation_repository)
    port = _FakeHostedPort()
    decider = _SpyDecider(ProactiveDecision(True, "ok", "睡了嗎？"))
    dispatcher = _dispatcher(
        harness, port=port, decider=decider,
        conversation_repository=conversations,
    )

    attempt = await dispatcher.evaluate(
        character_id=character.id, trigger=ProactiveTrigger.TICK,
    )

    # The send already happened; the first append lost the race and the bounded
    # retry recorded the turn exactly once.
    assert attempt.outcome == ProactiveOutcome.SENT
    assert conversations.append_calls == 2
    conversation = await harness.conversation_repository.latest_for_character(
        character.id, source="line",
    )
    assert conversation is not None
    assert [m.content for m in conversation.messages] == ["睡了嗎？"]


@pytest.mark.asyncio
async def test_deliver_pre_composed_routes_hosted() -> None:
    harness = build_messaging_harness()
    character = await _enable_hosted_character(harness)
    ledger = InMemoryExternalProactiveEventRepository()
    port = _FakeHostedPort()
    # deliver_pre_composed owns its own text — the decider is never consulted.
    decider = _SpyDecider(ProactiveDecision(False, "unused", None))
    dispatcher = _dispatcher(
        harness, port=port, decider=decider, ledger=ledger,
    )

    attempt = await dispatcher.deliver_pre_composed(
        character_id=character.id,
        text="約好的晚安訊息",
        trigger=ProactiveTrigger.SCHEDULED_PROMISE,
    )

    assert attempt.outcome == ProactiveOutcome.SENT
    assert decider.calls == 0
    assert len(port.accepted) == 1
    assert (
        port.accepted[0].tenant_id,
        port.accepted[0].account_id,
    ) == _HOSTED_IDENTITY
    assert port.accepted[0].text == "約好的晚安訊息"
    conversation = await harness.conversation_repository.latest_for_character(
        character.id, source="line",
    )
    assert conversation is not None
    assert [m.content for m in conversation.messages] == ["約好的晚安訊息"]


# --------------------------- identity resolver ------------------------------ #

def _cloud_operator(**overrides) -> OperatorProfile:
    fields = {
        "id": "default",
        "display_name": "Owner",
        "auth_provider": "cloud",
        "cloud_tenant_id": "tenant-7",
        "cloud_account_id": "account-9",
    }
    fields.update(overrides)
    return OperatorProfile(**fields)


@pytest.mark.asyncio
async def test_identity_resolver_maps_cloud_operator() -> None:
    harness = build_messaging_harness()
    character = await create_character(harness)
    operators = InMemoryOperatorProfileRepository()
    await operators.save(_cloud_operator())
    resolve = build_hosted_delivery_identity_resolver(
        character_repository=harness.character_repository,
        operator_repository=operators,
    )
    assert await resolve(character.id) == ("tenant-7", "account-9")


@pytest.mark.asyncio
async def test_identity_resolver_none_without_cloud_projection() -> None:
    harness = build_messaging_harness()
    character = await create_character(harness)
    operators = InMemoryOperatorProfileRepository()
    resolve = build_hosted_delivery_identity_resolver(
        character_repository=harness.character_repository,
        operator_repository=operators,
    )

    # No operator row at all.
    assert await resolve(character.id) is None

    # A local (non-cloud) operator never routes to a channel.
    await operators.save(
        _cloud_operator(auth_provider="local"),
    )
    assert await resolve(character.id) is None

    # A cloud operator missing a projection half is fail-closed.
    await operators.save(_cloud_operator(cloud_account_id=None))
    assert await resolve(character.id) is None


@pytest.mark.asyncio
async def test_identity_resolver_none_for_unknown_character() -> None:
    resolve = build_hosted_delivery_identity_resolver(
        character_repository=InMemoryCharacterRepository(),
        operator_repository=InMemoryOperatorProfileRepository(),
    )
    assert await resolve("ghost") is None
