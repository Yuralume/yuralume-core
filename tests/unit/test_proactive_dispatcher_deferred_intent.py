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
    DeferredIntent,
)
from kokoro_link.domain.entities.operator_profile import DEFAULT_OPERATOR_ID
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
from kokoro_link.infrastructure.proactive.heuristic_gate import (
    HeuristicProactiveGate,
)


_NOW = datetime(2026, 5, 21, 4, 0, tzinfo=timezone.utc)


def _daytime_gate() -> HeuristicProactiveGate:
    """Real gate with the night floor disabled, so the cooldown branch is
    the only thing under test (``_NOW`` sits inside the default 00-07
    quiet window)."""
    return HeuristicProactiveGate(
        local_tz=timezone.utc, quiet_hour_start=0, quiet_hour_end=0,
    )


def _skip_at(revisit_at_iso: str) -> ProactiveIntentionDecision:
    """The reported bug's verdict: hold back *because* a known moment is
    the right one."""
    return ProactiveIntentionDecision(
        should_consume_slot=False,
        reason="已經約好七點半，等到時間再自然聯絡",
        inner_motive="說好七點半一起上線核對遊戲任務",
        conversation_purpose="赴約",
        expected_reply="對方上線一起看任務",
        best_timing="evening",
        revisit_at_iso=revisit_at_iso,
    )


def _judge_unavailable() -> ProactiveIntentionDecision:
    """What the judge returns when it could not judge at all — an upstream
    failure or unusable output. Carries no motive and no appointment,
    because nothing was ever evaluated."""
    return ProactiveIntentionDecision(
        should_consume_slot=False,
        reason="intention judge LLM call failed",
        judge_unavailable=True,
    )


def _local_iso(when: datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M")


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
    gate: ProactiveGatePort | None = None,
    character_repo: InMemoryCharacterRepository | None = None,
    attempt_repo: InMemoryProactiveAttemptRepository | None = None,
) -> tuple[ProactiveDispatcher, _CapturingJudge, InMemoryCharacterRepository]:
    """``character_repo`` / ``attempt_repo`` are injectable so a
    multi-tick scenario can share the cooldown anchor across dispatchers
    the way the real scheduler does."""
    character_repo = character_repo or InMemoryCharacterRepository()
    attempt_repo = attempt_repo or InMemoryProactiveAttemptRepository()
    judge = _CapturingJudge(intention_decision)
    dispatcher = ProactiveDispatcher(
        character_repository=character_repo,
        conversation_repository=InMemoryConversationRepository(),
        account_repository=InMemoryMessagingAccountRepository(),
        binding_repository=InMemoryChannelBindingRepository(),
        attempt_repository=attempt_repo,
        gate=gate or _AlwaysPassGate(),
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


# ---------------------------------------------------------------------
# T2 — deferred intent carrying an explicit moment gets one pass through
# the cooldown when that moment arrives.
# ---------------------------------------------------------------------


_ALARM = _NOW + timedelta(minutes=8)


def _service() -> tuple[DeferredIntentService, InMemoryDeferredIntentRepository]:
    repo = InMemoryDeferredIntentRepository()
    return DeferredIntentService(
        repository=repo, settings=HumanizationSettings(),
    ), repo


@pytest.mark.asyncio
async def test_skip_with_explicit_moment_parks_the_alarm() -> None:
    svc, repo = _service()
    dispatcher, _, char_repo = _dispatcher(
        intention_decision=_skip_at(_local_iso(_ALARM)),
        deferred_service=svc,
    )
    character = await _build_character(char_repo)

    attempt = await dispatcher.evaluate(
        character_id=character.id, trigger=ProactiveTrigger.TICK, now=_NOW,
    )

    assert attempt.outcome == ProactiveOutcome.INTENTION_SKIPPED
    assert repo.snapshot()[0].revisit_at == _ALARM


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "2020-01-01T19:30",   # already past — the moment cannot be kept
        "晚上七點半",           # natural language, not a timestamp
        "2026-13-45T99:99",   # syntactically ISO-ish, semantically junk
    ],
)
@pytest.mark.asyncio
async def test_unusable_moment_parks_the_motive_without_an_alarm(
    raw: str,
) -> None:
    """A stale or malformed timestamp must never buy an exemption — but
    it must not cost the motive either; it parks exactly as before T2."""
    svc, repo = _service()
    dispatcher, _, char_repo = _dispatcher(
        intention_decision=_skip_at(raw), deferred_service=svc,
    )
    character = await _build_character(char_repo)

    await dispatcher.evaluate(
        character_id=character.id, trigger=ProactiveTrigger.TICK, now=_NOW,
    )

    parked = repo.snapshot()[0]
    assert parked.revisit_at is None
    assert parked.inner_motive == "說好七點半一起上線核對遊戲任務"


@pytest.mark.asyncio
async def test_due_alarm_carries_the_tick_through_the_cooldown() -> None:
    """The reported bug: the 19:22 skip advances the cooldown anchor, so
    without the exemption the 19:30 appointment is silently blanked."""
    svc, repo = _service()
    attempts = InMemoryProactiveAttemptRepository()
    char_repo = InMemoryCharacterRepository()
    character = await _build_character(char_repo)

    first_dispatcher, _, _ = _dispatcher(
        intention_decision=_skip_at(_local_iso(_ALARM)),
        deferred_service=svc,
        gate=_daytime_gate(),
        character_repo=char_repo,
        attempt_repo=attempts,
    )
    first = await first_dispatcher.evaluate(
        character_id=character.id, trigger=ProactiveTrigger.TICK, now=_NOW,
    )
    assert first.outcome == ProactiveOutcome.INTENTION_SKIPPED

    # 8 minutes later — deep inside the 30-minute cooldown.
    second_dispatcher, judge2, _ = _dispatcher(
        intention_decision=_skip_at(""),
        deferred_service=svc,
        gate=_daytime_gate(),
        character_repo=char_repo,
        attempt_repo=attempts,
    )
    second = await second_dispatcher.evaluate(
        character_id=character.id, trigger=ProactiveTrigger.TICK, now=_ALARM,
    )

    # Reaching the judge at all is the assertion: the gate let it past.
    assert second.outcome == ProactiveOutcome.INTENTION_SKIPPED
    assert [i.inner_motive for i in judge2.received_intents] == [
        "說好七點半一起上線核對遊戲任務",
    ]
    # And the alarm was spent on the way through.
    assert [row.revisit_at for row in repo.snapshot()] == [None, None]


@pytest.mark.asyncio
async def test_due_tick_shows_the_judge_that_the_moment_has_arrived() -> None:
    """F2-1 — the alarm is spent before the context is assembled, so every
    row re-read from the store comes back alarm-less. Without overlaying
    the pre-spend snapshot the judge sees exactly the picture it skipped
    on last time and can only skip again ("等約定時間") — while the moment
    it would re-name is now in the past and gets rejected on the way in.
    The signal that this tick *is* the appointment must reach the judge."""
    svc, _ = _service()
    attempts = InMemoryProactiveAttemptRepository()
    char_repo = InMemoryCharacterRepository()
    character = await _build_character(char_repo)

    first_dispatcher, _, _ = _dispatcher(
        intention_decision=_skip_at(_local_iso(_ALARM)),
        deferred_service=svc,
        gate=_daytime_gate(),
        character_repo=char_repo,
        attempt_repo=attempts,
    )
    await first_dispatcher.evaluate(
        character_id=character.id, trigger=ProactiveTrigger.TICK, now=_NOW,
    )

    second_dispatcher, judge2, _ = _dispatcher(
        intention_decision=_skip_at(""),
        deferred_service=svc,
        gate=_daytime_gate(),
        character_repo=char_repo,
        attempt_repo=attempts,
    )
    await second_dispatcher.evaluate(
        character_id=character.id, trigger=ProactiveTrigger.TICK, now=_ALARM,
    )

    assert [i.revisit_at for i in judge2.received_intents] == [_ALARM]


@pytest.mark.asyncio
async def test_judge_failure_does_not_burn_the_alarm() -> None:
    """F2-3 — the judge fail-soft path returns should_consume_slot=False
    without ever evaluating anything, and records nothing (empty motive).
    If the alarm stayed spent, one upstream 5xx on the appointment tick
    would lose the appointment for good."""
    svc, repo = _service()
    attempts = InMemoryProactiveAttemptRepository()
    char_repo = InMemoryCharacterRepository()
    character = await _build_character(char_repo)

    first_dispatcher, _, _ = _dispatcher(
        intention_decision=_skip_at(_local_iso(_ALARM)),
        deferred_service=svc,
        gate=_daytime_gate(),
        character_repo=char_repo,
        attempt_repo=attempts,
    )
    await first_dispatcher.evaluate(
        character_id=character.id, trigger=ProactiveTrigger.TICK, now=_NOW,
    )

    # The appointment tick — upstream is down.
    broken_dispatcher, _, _ = _dispatcher(
        intention_decision=_judge_unavailable(),
        deferred_service=svc,
        gate=_daytime_gate(),
        character_repo=char_repo,
        attempt_repo=attempts,
    )
    await broken_dispatcher.evaluate(
        character_id=character.id, trigger=ProactiveTrigger.TICK, now=_ALARM,
    )

    assert [row.revisit_at for row in repo.snapshot()] == [_ALARM]

    # Next tick, still inside the cooldown: the alarm buys the look the
    # broken tick never got.
    retry_dispatcher, judge3, _ = _dispatcher(
        intention_decision=_skip_at(""),
        deferred_service=svc,
        gate=_daytime_gate(),
        character_repo=char_repo,
        attempt_repo=attempts,
    )
    third = await retry_dispatcher.evaluate(
        character_id=character.id,
        trigger=ProactiveTrigger.TICK,
        now=_ALARM + timedelta(minutes=2),
    )
    assert third.outcome == ProactiveOutcome.INTENTION_SKIPPED
    assert [i.inner_motive for i in judge3.received_intents] == [
        "說好七點半一起上線核對遊戲任務",
    ]


class _RaisingJudge(ProactiveIntentionJudgePort):
    """What upstream looks like when it doesn't fail soft — the ``judge``
    coroutine itself blows up, so the dispatcher's ``except Exception``
    branch is what runs, not the ``judge_unavailable=True`` return path."""

    async def judge(
        self, context: ProactiveContext,
    ) -> ProactiveIntentionDecision:
        raise RuntimeError("upstream on fire")


@pytest.mark.asyncio
async def test_judge_exception_does_not_burn_the_alarm() -> None:
    """F2-3's sibling: nothing evaluated this tick either — the judge
    raised before returning anything — so the ERRORED branch must restore
    the alarm exactly like the fail-soft ``judge_unavailable=True`` branch
    does, or one upstream crash on the appointment tick loses it for good.
    """
    svc, repo = _service()
    attempts = InMemoryProactiveAttemptRepository()
    char_repo = InMemoryCharacterRepository()
    character = await _build_character(char_repo)

    first_dispatcher, _, _ = _dispatcher(
        intention_decision=_skip_at(_local_iso(_ALARM)),
        deferred_service=svc,
        gate=_daytime_gate(),
        character_repo=char_repo,
        attempt_repo=attempts,
    )
    await first_dispatcher.evaluate(
        character_id=character.id, trigger=ProactiveTrigger.TICK, now=_NOW,
    )
    assert repo.snapshot()[0].revisit_at == _ALARM

    # The appointment tick — the judge itself crashes.
    raising_dispatcher = ProactiveDispatcher(
        character_repository=char_repo,
        conversation_repository=InMemoryConversationRepository(),
        account_repository=InMemoryMessagingAccountRepository(),
        binding_repository=InMemoryChannelBindingRepository(),
        attempt_repository=attempts,
        gate=_daytime_gate(),
        decider=_SendingDecider(),
        adapters={},
        intention_judge=_RaisingJudge(),
        deferred_intent_service=svc,
    )
    second = await raising_dispatcher.evaluate(
        character_id=character.id, trigger=ProactiveTrigger.TICK, now=_ALARM,
    )

    assert second.outcome == ProactiveOutcome.ERRORED
    assert second.reason == "intention judge raised"
    assert repo.snapshot()[0].revisit_at == _ALARM

    # Next tick, still inside the cooldown: the alarm buys the look the
    # crashed tick never got.
    retry_dispatcher, judge3, _ = _dispatcher(
        intention_decision=_skip_at(""),
        deferred_service=svc,
        gate=_daytime_gate(),
        character_repo=char_repo,
        attempt_repo=attempts,
    )
    third = await retry_dispatcher.evaluate(
        character_id=character.id,
        trigger=ProactiveTrigger.TICK,
        now=_ALARM + timedelta(minutes=2),
    )
    assert third.outcome == ProactiveOutcome.INTENTION_SKIPPED
    assert [i.inner_motive for i in judge3.received_intents] == [
        "說好七點半一起上線核對遊戲任務",
    ]


@pytest.mark.asyncio
async def test_semantic_skip_burns_the_alarm_even_with_a_motive() -> None:
    """The other side of F2-3: a judge that actually looked and still
    decided to hold back owns that call. Its alarm stays spent — only a
    fresh appointment it names on the way out re-arms anything."""
    svc, repo = _service()
    attempts = InMemoryProactiveAttemptRepository()
    char_repo = InMemoryCharacterRepository()
    character = await _build_character(char_repo)

    first_dispatcher, _, _ = _dispatcher(
        intention_decision=_skip_at(_local_iso(_ALARM)),
        deferred_service=svc,
        gate=_daytime_gate(),
        character_repo=char_repo,
        attempt_repo=attempts,
    )
    await first_dispatcher.evaluate(
        character_id=character.id, trigger=ProactiveTrigger.TICK, now=_NOW,
    )

    second_dispatcher, _, _ = _dispatcher(
        intention_decision=_skip_at(""),
        deferred_service=svc,
        gate=_daytime_gate(),
        character_repo=char_repo,
        attempt_repo=attempts,
    )
    await second_dispatcher.evaluate(
        character_id=character.id, trigger=ProactiveTrigger.TICK, now=_ALARM,
    )

    assert all(row.revisit_at is None for row in repo.snapshot())


@pytest.mark.asyncio
async def test_same_tick_is_cooldown_blocked_without_an_alarm() -> None:
    """Control for the test above — with no moment named, the identical
    second tick never reaches the judge."""
    svc, _ = _service()
    attempts = InMemoryProactiveAttemptRepository()
    char_repo = InMemoryCharacterRepository()
    character = await _build_character(char_repo)

    first_dispatcher, _, _ = _dispatcher(
        intention_decision=_skip_at(""),
        deferred_service=svc,
        gate=_daytime_gate(),
        character_repo=char_repo,
        attempt_repo=attempts,
    )
    await first_dispatcher.evaluate(
        character_id=character.id, trigger=ProactiveTrigger.TICK, now=_NOW,
    )

    second_dispatcher, judge2, _ = _dispatcher(
        intention_decision=_skip_at(""),
        deferred_service=svc,
        gate=_daytime_gate(),
        character_repo=char_repo,
        attempt_repo=attempts,
    )
    second = await second_dispatcher.evaluate(
        character_id=character.id, trigger=ProactiveTrigger.TICK, now=_ALARM,
    )

    assert second.outcome == ProactiveOutcome.GATE_BLOCKED
    assert "cooldown" in second.reason
    assert judge2.received_intents == ()


@pytest.mark.asyncio
async def test_exemption_is_single_use_and_the_cooldown_returns() -> None:
    """One alarm buys exactly one look. Otherwise a single parked motive
    would keep the character permanently out of cooldown and burn a
    judge call every tick."""
    svc, repo = _service()
    attempts = InMemoryProactiveAttemptRepository()
    char_repo = InMemoryCharacterRepository()
    character = await _build_character(char_repo)

    for decision, when in (
        (_skip_at(_local_iso(_ALARM)), _NOW),
        (_skip_at(""), _ALARM),
    ):
        dispatcher, _, _ = _dispatcher(
            intention_decision=decision,
            deferred_service=svc,
            gate=_daytime_gate(),
            character_repo=char_repo,
            attempt_repo=attempts,
        )
        await dispatcher.evaluate(
            character_id=character.id,
            trigger=ProactiveTrigger.TICK,
            now=when,
        )

    third_dispatcher, judge3, _ = _dispatcher(
        intention_decision=_skip_at(""),
        deferred_service=svc,
        gate=_daytime_gate(),
        character_repo=char_repo,
        attempt_repo=attempts,
    )
    third = await third_dispatcher.evaluate(
        character_id=character.id,
        trigger=ProactiveTrigger.TICK,
        now=_ALARM + timedelta(minutes=2),
    )

    assert third.outcome == ProactiveOutcome.GATE_BLOCKED
    assert "cooldown" in third.reason
    assert all(row.revisit_at is None for row in repo.snapshot())


@pytest.mark.asyncio
async def test_quiet_hours_are_not_exempted_and_the_alarm_survives() -> None:
    """The exemption waives the cooldown only. A 03:00 appointment still
    loses to the night floor — and because the gate never passed, the
    alarm is not spent, so it can fire once the window opens."""
    svc, repo = _service()
    char_repo = InMemoryCharacterRepository()
    character = await _build_character(char_repo)
    await repo.add(DeferredIntent.new(
        character_id=character.id,
        operator_id=DEFAULT_OPERATOR_ID,
        trigger="tick",
        inner_motive="說好七點半一起上線核對遊戲任務",
        revisit_at=_NOW - timedelta(minutes=1),
        now=_NOW - timedelta(minutes=30),
    ))

    dispatcher, judge, _ = _dispatcher(
        intention_decision=_skip_at(""),
        deferred_service=svc,
        # Default 00:00-07:00 night floor; _NOW is 04:00 UTC.
        gate=HeuristicProactiveGate(local_tz=timezone.utc),
        character_repo=char_repo,
    )
    attempt = await dispatcher.evaluate(
        character_id=character.id, trigger=ProactiveTrigger.TICK, now=_NOW,
    )

    assert attempt.outcome == ProactiveOutcome.GATE_BLOCKED
    assert "night-hours" in attempt.reason
    assert judge.received_intents == ()
    assert repo.snapshot()[0].revisit_at == _NOW - timedelta(minutes=1)


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
