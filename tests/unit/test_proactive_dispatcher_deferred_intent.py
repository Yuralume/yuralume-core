"""Integration tests for the §3.4 deferred-intent loop on the dispatcher.

Covers the three loop seams:

- ``record_if_useful`` fires on ``INTENTION_SKIPPED`` outcomes.
- ``list_active`` populates ``ProactiveContext.deferred_intents`` next tick.
- ``mark_consumed`` flips rows after a successful ``SENT``.

Reuses the in-memory dispatcher harness from ``test_proactive_dispatcher.py``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.application.services.deferred_intent_service import (
    DeferredIntentService,
)
from kokoro_link.application.services.proactive_dispatcher import (
    ProactiveDispatcher,
)
from kokoro_link.bootstrap.settings import HumanizationSettings
from kokoro_link.contracts.proactive import (
    GateVerdict,
    ProactiveContext,
    ProactiveDecision,
    ProactiveDeciderPort,
    ProactiveGatePort,
)
from kokoro_link.contracts.proactive_intention import (
    ProactiveIntentionDecision,
    ProactiveIntentionJudgePort,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.deferred_intent import (
    STATUS_ACTIVE,
    STATUS_CONSUMED,
)
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.proactive_outcome import ProactiveOutcome
from kokoro_link.domain.value_objects.proactive_trigger import ProactiveTrigger
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_conversations import (
    InMemoryConversationRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_deferred_intents import (
    InMemoryDeferredIntentRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_messaging_accounts import (
    InMemoryMessagingAccountRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_proactive_attempts import (
    InMemoryProactiveAttemptRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_channel_bindings import (
    InMemoryChannelBindingRepository,
)


_NOW = datetime(2026, 5, 21, 4, 0, tzinfo=timezone.utc)


class _AlwaysPassGate(ProactiveGatePort):
    async def check(self, **_):  # noqa: ANN401, ANN003
        return GateVerdict(passed=True, reason="ok")


class _SendingDecider(ProactiveDeciderPort):
    async def decide(self, context: ProactiveContext) -> ProactiveDecision:
        return ProactiveDecision(
            should_send=True, reason="ok", message="嗨",
        )


class _CapturingJudge(ProactiveIntentionJudgePort):
    def __init__(self, decision: ProactiveIntentionDecision) -> None:
        self._decision = decision
        self.received_intents: tuple = ()

    async def judge(
        self, context: ProactiveContext,
    ) -> ProactiveIntentionDecision:
        self.received_intents = context.deferred_intents
        return self._decision


async def _build_character(repo: InMemoryCharacterRepository) -> Character:
    character = Character.create(
        name="Mio",
        summary="咖啡店打工的大學生",
        personality=["溫柔"],
        interests=["吉他"],
        speaking_style="輕柔",
        boundaries=[],
        state=CharacterState(
            emotion="平靜",
            affection=60,
            fatigue=20,
            trust=60,
            energy=80,
            last_active_at=_NOW - timedelta(hours=1),
        ),
        proactive_enabled=True,
        proactive_daily_limit=5,
        accepts_web_proactive=True,
    )
    await repo.save(character)
    return character


def _dispatcher(
    *,
    intention_decision: ProactiveIntentionDecision,
    deferred_service: DeferredIntentService,
) -> tuple[ProactiveDispatcher, _CapturingJudge, InMemoryCharacterRepository]:
    character_repo = InMemoryCharacterRepository()
    attempt_repo = InMemoryProactiveAttemptRepository()
    judge = _CapturingJudge(intention_decision)
    dispatcher = ProactiveDispatcher(
        character_repository=character_repo,
        conversation_repository=InMemoryConversationRepository(),
        account_repository=InMemoryMessagingAccountRepository(),
        binding_repository=InMemoryChannelBindingRepository(),
        attempt_repository=attempt_repo,
        gate=_AlwaysPassGate(),
        decider=_SendingDecider(),
        adapters={},
        intention_judge=judge,
        deferred_intent_service=deferred_service,
    )
    return dispatcher, judge, character_repo


@pytest.mark.asyncio
async def test_intention_skip_records_deferred_intent_with_motive() -> None:
    repo = InMemoryDeferredIntentRepository()
    svc = DeferredIntentService(
        repository=repo, settings=HumanizationSettings(),
    )
    skip_decision = ProactiveIntentionDecision(
        should_consume_slot=False,
        reason="現在不合適",
        inner_motive="想關心對方面試的後續",
        conversation_purpose="自然延續上次的話題",
        expected_reply="對方說個近況",
        risk="會像刷存在感",
        best_timing="evening",
    )
    dispatcher, _, char_repo = _dispatcher(
        intention_decision=skip_decision, deferred_service=svc,
    )
    character = await _build_character(char_repo)

    attempt = await dispatcher.evaluate(
        character_id=character.id,
        trigger=ProactiveTrigger.TICK,
        now=_NOW,
    )

    assert attempt.outcome == ProactiveOutcome.INTENTION_SKIPPED
    snap = repo.snapshot()
    assert len(snap) == 1
    assert snap[0].status == STATUS_ACTIVE
    assert snap[0].inner_motive == "想關心對方面試的後續"


@pytest.mark.asyncio
async def test_intention_skip_without_motive_does_not_record() -> None:
    repo = InMemoryDeferredIntentRepository()
    svc = DeferredIntentService(
        repository=repo, settings=HumanizationSettings(),
    )
    skip_decision = ProactiveIntentionDecision(
        should_consume_slot=False,
        reason="只有素材沒動機",
        inner_motive="",
        risk="像新聞推播",
    )
    dispatcher, _, char_repo = _dispatcher(
        intention_decision=skip_decision, deferred_service=svc,
    )
    character = await _build_character(char_repo)

    await dispatcher.evaluate(
        character_id=character.id,
        trigger=ProactiveTrigger.TICK,
        now=_NOW,
    )
    assert repo.snapshot() == []


@pytest.mark.asyncio
async def test_next_tick_surfaces_active_intents_and_marks_consumed_on_send() -> None:
    """End-to-end loop: first tick parks a motive; second tick the judge
    sees it in the context and approves; SENT outcome flips the row to
    consumed so it does not re-surface on tick 3."""
    repo = InMemoryDeferredIntentRepository()
    svc = DeferredIntentService(
        repository=repo, settings=HumanizationSettings(),
    )

    # Tick 1 — judge skips, motive parked.
    skip_decision = ProactiveIntentionDecision(
        should_consume_slot=False,
        reason="現在剛聊完",
        inner_motive="想接著聊閱讀感",
        conversation_purpose="延續閱讀話題",
    )
    dispatcher1, judge1, char_repo = _dispatcher(
        intention_decision=skip_decision, deferred_service=svc,
    )
    character = await _build_character(char_repo)
    await dispatcher1.evaluate(
        character_id=character.id,
        trigger=ProactiveTrigger.TICK,
        now=_NOW,
    )
    assert judge1.received_intents == ()
    assert len(repo.snapshot()) == 1

    # Tick 2 — same character (re-saved in another harness for isolation),
    # judge now approves; sent outcome consumes the parked motive.
    approve_decision = ProactiveIntentionDecision(
        should_consume_slot=True,
        reason="時機到了",
        inner_motive="想接著聊閱讀感",
        conversation_purpose="延續閱讀話題",
    )
    dispatcher2, judge2, char_repo2 = _dispatcher(
        intention_decision=approve_decision, deferred_service=svc,
    )
    await char_repo2.save(character)
    next_when = _NOW + timedelta(minutes=20)
    second_attempt = await dispatcher2.evaluate(
        character_id=character.id,
        trigger=ProactiveTrigger.TICK,
        now=next_when,
    )
    assert second_attempt.outcome == ProactiveOutcome.SENT
    assert len(judge2.received_intents) == 1
    assert judge2.received_intents[0].inner_motive == "想接著聊閱讀感"

    snap = repo.snapshot()
    assert len(snap) == 1
    assert snap[0].status == STATUS_CONSUMED


@pytest.mark.asyncio
async def test_disabled_feature_flag_skips_record_and_inject() -> None:
    repo = InMemoryDeferredIntentRepository()
    svc = DeferredIntentService(
        repository=repo,
        settings=HumanizationSettings(deferred_intent_enabled=False),
    )
    skip_decision = ProactiveIntentionDecision(
        should_consume_slot=False,
        reason="現在不合適",
        inner_motive="想說話",
    )
    dispatcher, judge, char_repo = _dispatcher(
        intention_decision=skip_decision, deferred_service=svc,
    )
    character = await _build_character(char_repo)
    await dispatcher.evaluate(
        character_id=character.id,
        trigger=ProactiveTrigger.TICK,
        now=_NOW,
    )
    assert repo.snapshot() == []
    assert judge.received_intents == ()
