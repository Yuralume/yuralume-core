"""End-to-end BDD for the proactive dispatcher pipeline.

The in-memory harness gives us a fully-wired dispatcher, fake adapters,
and real heuristic gate. We stub the decider per-test so we can steer
between SENT / SKIPPED paths without involving an LLM.
"""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from kokoro_link.application.services.pre_message_proactive_budget import (
    PRE_MESSAGE_BUDGET_UNAVAILABLE_REASON,
    PRE_MESSAGE_CAP_REASON,
    PRE_MESSAGE_DELAY_REASON,
    PRE_MESSAGE_INTERVAL_REASON,
    PRE_MESSAGE_PROACTIVE_CAP,
    PRE_MESSAGE_PROACTIVE_DELAY_HOURS,
)
from kokoro_link.application.services.proactive_dispatcher import ProactiveDispatcher
from kokoro_link.contracts.proactive import (
    ProactiveContext,
    ProactiveDecision,
    ProactiveDeciderPort,
)
from kokoro_link.contracts.proactive_intention import (
    ProactiveIntentionDecision,
    ProactiveIntentionJudgePort,
)
from kokoro_link.contracts.messaging import OutboundAttachment
from kokoro_link.domain.entities.conversation import (
    Conversation,
    Message,
    MessageRole,
)
from kokoro_link.domain.entities.character_operator_relationship_seed import (
    CharacterOperatorRelationshipSeed,
)
from kokoro_link.domain.entities.operator_profile import DEFAULT_OPERATOR_ID
from kokoro_link.domain.entities.proactive_attempt import ProactiveAttempt
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.entities.operator_profile import OperatorProfile
from kokoro_link.domain.value_objects.platform import Platform
from kokoro_link.domain.value_objects.proactive_outcome import ProactiveOutcome
from kokoro_link.domain.value_objects.proactive_trigger import ProactiveTrigger
from kokoro_link.infrastructure.proactive.heuristic_gate import HeuristicProactiveGate
from kokoro_link.infrastructure.observability.turn_recorder import BackgroundTurnRecorder
from kokoro_link.infrastructure.repositories.in_memory_emotion_events import (
    InMemoryEmotionEventRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_proactive_attempts import (
    InMemoryProactiveAttemptRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_initial_relationship import (
    InMemoryCharacterOperatorRelationshipSeedRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_turn_records import (
    InMemoryTurnRecordRepository,
)
from tests.unit._messaging_harness import (
    build_messaging_harness,
    create_character,
    create_telegram_account,
)


class _FixedDecider(ProactiveDeciderPort):
    def __init__(self, decision: ProactiveDecision) -> None:
        self._decision = decision
        self.calls: list[ProactiveContext] = []

    async def decide(self, context: ProactiveContext) -> ProactiveDecision:
        self.calls.append(context)
        return self._decision


class _FixedIntentionJudge(ProactiveIntentionJudgePort):
    def __init__(self, decision: ProactiveIntentionDecision) -> None:
        self._decision = decision
        self.calls: list[ProactiveContext] = []

    async def judge(
        self, context: ProactiveContext,
    ) -> ProactiveIntentionDecision:
        self.calls.append(context)
        return self._decision


class _OperatorProfileService:
    def __init__(self, timezone_id: str) -> None:
        self.timezone_id = timezone_id

    async def get_for_user(self, user_id: str) -> OperatorProfile:
        return OperatorProfile(
            id=user_id,
            display_name=user_id,
            timezone_id=self.timezone_id,
        )


def _make_dispatcher(
    harness,
    *,
    decider: ProactiveDeciderPort,
    intention_judge: ProactiveIntentionJudgePort | None = None,
    attempts: InMemoryProactiveAttemptRepository | None = None,
    relationship_seed_repository: (
        InMemoryCharacterOperatorRelationshipSeedRepository | None
    ) = None,
    turn_recorder=None,
    emotion_event_repository=None,
    prompt_pack_hash_provider=None,
    subscription_access_guard=None,
) -> tuple[ProactiveDispatcher, InMemoryProactiveAttemptRepository]:
    attempt_repo = attempts or InMemoryProactiveAttemptRepository()
    dispatcher = ProactiveDispatcher(
        character_repository=harness.character_repository,
        conversation_repository=harness.conversation_repository,
        account_repository=harness.account_repository,
        binding_repository=harness.binding_repository,
        attempt_repository=attempt_repo,
        # Pin local_tz to UTC so the night-hours floor doesn't flake the
        # suite when the wall clock happens to cross midnight in the
        # operator's real timezone mid-run.
        gate=HeuristicProactiveGate(local_tz=timezone.utc, quiet_hour_start=0, quiet_hour_end=0),
        decider=decider,
        intention_judge=intention_judge,
        adapters={
            Platform.TELEGRAM: harness.telegram_adapter,
            Platform.LINE: harness.line_adapter,
        },
        turn_recorder=turn_recorder,
        emotion_event_repository=emotion_event_repository,
        relationship_seed_repository=relationship_seed_repository,
        prompt_pack_hash_provider=prompt_pack_hash_provider,
        subscription_access_guard=subscription_access_guard,
    )
    return dispatcher, attempt_repo


class _DenySubscriptionGuard:
    async def is_character_allowed(self, character) -> bool:
        return False


async def _enable_character(
    harness,
    *,
    idle_minutes: float = 60.0,
    accepts_web_proactive: bool = False,
):
    dto = await create_character(harness)
    character = await harness.character_repository.get(dto.id)
    assert character is not None
    updated = _character_with_proactive(
        character,
        idle_minutes=idle_minutes,
        accepts_web_proactive=accepts_web_proactive,
    )
    await harness.character_repository.save(updated)
    return updated


def _character_with_proactive(
    character,
    *,
    idle_minutes: float | None = 60.0,
    accepts_web_proactive: bool = False,
):
    """Flip proactive_enabled on and set idle time for the gate.

    Web delivery is *off* by default in tests so existing NO_BINDING /
    routing assertions keep their single-target semantics. Tests that
    exercise the web fan-out path opt in explicitly.
    """
    now = datetime.now(timezone.utc)
    last_active = (
        None if idle_minutes is None
        else now - timedelta(minutes=idle_minutes)
    )
    return character.update(
        name=None, summary=None, personality=None, interests=None,
        speaking_style=None, boundaries=None, aspirations=None, appearance=None,
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
            last_active_at=last_active,
        ),
        proactive_enabled=True,
        accepts_web_proactive=accepts_web_proactive,
    )


@pytest.mark.asyncio
async def test_manual_proactive_is_disabled_before_decider_when_tenant_locked() -> None:
    harness = build_messaging_harness()
    character = await _enable_character(harness)
    decider = _FixedDecider(ProactiveDecision(True, "ok", "blocked"))
    dispatcher, _ = _make_dispatcher(
        harness,
        decider=decider,
        subscription_access_guard=_DenySubscriptionGuard(),
    )

    attempt = await dispatcher.evaluate(
        character_id=character.id,
        trigger=ProactiveTrigger.from_string("manual"),
    )

    assert attempt.outcome == ProactiveOutcome.DISABLED
    assert attempt.reason == "subscription is inactive"
    assert decider.calls == []


@pytest.mark.asyncio
async def test_waits_for_first_user_message_before_proactive_decider() -> None:
    harness = build_messaging_harness()
    character = await _enable_character(
        harness, idle_minutes=None, accepts_web_proactive=True,
    )
    decider = _FixedDecider(ProactiveDecision(True, "ok", "hi"))
    dispatcher, _ = _make_dispatcher(harness, decider=decider)

    attempt = await dispatcher.evaluate(
        character_id=character.id, trigger=ProactiveTrigger.TICK,
    )

    assert attempt.outcome == ProactiveOutcome.GATE_BLOCKED
    assert attempt.reason == "waiting for first user message"
    assert decider.calls == []
    assert harness.telegram_adapter.sent == []


@pytest.mark.asyncio
async def test_pre_message_proactive_requires_explicit_seed_permission() -> None:
    harness = build_messaging_harness()
    character = await _enable_character(
        harness, idle_minutes=None, accepts_web_proactive=True,
    )
    relationship_repo = InMemoryCharacterOperatorRelationshipSeedRepository()
    await relationship_repo.save(
        CharacterOperatorRelationshipSeed(
            character_id=character.id,
            operator_id=DEFAULT_OPERATOR_ID,
            relationship_label="剛認識",
            proactive_permission=True,
            proactive_cadence_hint="一天最多一次，下午較好",
        ),
    )
    decider = _FixedDecider(ProactiveDecision(True, "ok", "你好，小夏"))
    dispatcher, _ = _make_dispatcher(
        harness,
        decider=decider,
        relationship_seed_repository=relationship_repo,
    )

    attempt = await dispatcher.evaluate(
        character_id=character.id,
        trigger=ProactiveTrigger.TICK,
        now=datetime(2026, 4, 18, 14, 30, tzinfo=timezone.utc),
    )

    relationship_lines = "\n".join(decider.calls[0].initial_relationship_lines)
    assert attempt.outcome == ProactiveOutcome.SENT
    assert len(decider.calls) == 1
    assert "剛認識" in relationship_lines
    assert "一天最多一次" in relationship_lines


@pytest.mark.asyncio
async def test_pre_message_proactive_seed_permission_uses_character_owner() -> None:
    harness = build_messaging_harness()
    character = await _enable_character(
        harness, idle_minutes=None, accepts_web_proactive=True,
    )
    character = replace(character, user_id="alice")
    await harness.character_repository.save(character)
    relationship_repo = InMemoryCharacterOperatorRelationshipSeedRepository()
    await relationship_repo.save(
        CharacterOperatorRelationshipSeed(
            character_id=character.id,
            operator_id="alice",
            relationship_label="創作搭檔",
            proactive_permission=True,
            proactive_cadence_hint="下午偶爾問候，最多一天一次",
        ),
    )
    decider = _FixedDecider(ProactiveDecision(True, "ok", "下午好"))
    dispatcher, _ = _make_dispatcher(
        harness,
        decider=decider,
        relationship_seed_repository=relationship_repo,
    )

    attempt = await dispatcher.evaluate(
        character_id=character.id,
        trigger=ProactiveTrigger.TICK,
        now=datetime(2026, 4, 18, 14, 30, tzinfo=timezone.utc),
    )

    relationship_lines = "\n".join(decider.calls[0].initial_relationship_lines)
    assert attempt.outcome == ProactiveOutcome.SENT
    assert "創作搭檔" in relationship_lines
    assert "最多一天一次" in relationship_lines


@pytest.mark.asyncio
async def test_pre_message_proactive_permission_without_cadence_now_sends() -> None:
    """IR4: cadence hint is advisory only — permission alone unblocks.

    Before IR4 this was the trap: creation UI let an operator check "can
    find me first" without ever saying the frequency hint was secretly
    co-required, so the character stayed silent forever with no signal
    anywhere that anything was still missing. The gate now tracks
    ``proactive_permission`` alone.
    """
    harness = build_messaging_harness()
    character = await _enable_character(
        harness, idle_minutes=None, accepts_web_proactive=True,
    )
    relationship_repo = InMemoryCharacterOperatorRelationshipSeedRepository()
    await relationship_repo.save(
        CharacterOperatorRelationshipSeed(
            character_id=character.id,
            operator_id=DEFAULT_OPERATOR_ID,
            relationship_label="朋友",
            proactive_permission=True,
        ),
    )
    decider = _FixedDecider(ProactiveDecision(True, "ok", "hi"))
    dispatcher, _ = _make_dispatcher(
        harness,
        decider=decider,
        relationship_seed_repository=relationship_repo,
    )

    attempt = await dispatcher.evaluate(
        character_id=character.id,
        trigger=ProactiveTrigger.TICK,
        now=datetime(2026, 4, 18, 14, 30, tzinfo=timezone.utc),
    )

    relationship_lines = "\n".join(decider.calls[0].initial_relationship_lines)
    assert attempt.outcome == ProactiveOutcome.SENT
    assert len(decider.calls) == 1
    assert "朋友" in relationship_lines
    # Permission line still renders; the now-optional cadence line does not
    # (render_initial_relationship_seed_lines skips it when blank), so the
    # character sees no fabricated frequency and has to judge it itself.
    assert "主動訊息授權" in relationship_lines
    assert "主動訊息頻率或時機" not in relationship_lines


@pytest.mark.asyncio
async def test_pre_message_proactive_without_permission_still_waits_even_with_cadence() -> None:
    """A filled-in cadence hint never substitutes for the permission flag."""
    harness = build_messaging_harness()
    character = await _enable_character(
        harness, idle_minutes=None, accepts_web_proactive=True,
    )
    relationship_repo = InMemoryCharacterOperatorRelationshipSeedRepository()
    await relationship_repo.save(
        CharacterOperatorRelationshipSeed(
            character_id=character.id,
            operator_id=DEFAULT_OPERATOR_ID,
            relationship_label="朋友",
            proactive_permission=False,
            proactive_cadence_hint="一天最多一次",
        ),
    )
    decider = _FixedDecider(ProactiveDecision(True, "ok", "hi"))
    dispatcher, _ = _make_dispatcher(
        harness,
        decider=decider,
        relationship_seed_repository=relationship_repo,
    )

    attempt = await dispatcher.evaluate(
        character_id=character.id,
        trigger=ProactiveTrigger.TICK,
    )

    assert attempt.outcome == ProactiveOutcome.GATE_BLOCKED
    assert attempt.reason == "waiting for first user message"
    assert decider.calls == []


async def _pre_message_dispatcher(
    harness,
    *,
    decider: ProactiveDeciderPort,
    attempts: InMemoryProactiveAttemptRepository | None = None,
):
    """A character the player has never spoken to, permitted to reach out."""
    relationship_repo = InMemoryCharacterOperatorRelationshipSeedRepository()
    dispatcher, attempt_repo = _make_dispatcher(
        harness,
        decider=decider,
        attempts=attempts,
        relationship_seed_repository=relationship_repo,
    )
    return dispatcher, attempt_repo, relationship_repo


async def _permit_pre_message(relationship_repo, character_id: str) -> None:
    await relationship_repo.save(
        CharacterOperatorRelationshipSeed(
            character_id=character_id,
            operator_id=DEFAULT_OPERATOR_ID,
            relationship_label="朋友",
            proactive_permission=True,
        ),
    )


def _past_pre_message_send(character_id: str, at: datetime):
    return ProactiveAttempt.record(
        character_id=character_id,
        trigger=ProactiveTrigger.TICK,
        outcome=ProactiveOutcome.SENT,
        reason="ok",
        message="在嗎",
        now=at,
    )


@pytest.mark.asyncio
async def test_pre_message_cap_allows_the_first_two_then_blocks(
) -> None:
    """TR2-A: 0 and 1 prior pushes pass; the cap-th one stops the run.

    Anti-nag before the player ever speaks cannot lean on the unanswered
    streak — that signal is pinned to 0 while ``idle_minutes`` is None —
    so the ceiling is a plain total, and once spent no amount of elapsed
    time buys another push back.
    """
    harness = build_messaging_harness()
    character = await _enable_character(
        harness, idle_minutes=None, accepts_web_proactive=True,
    )
    now = datetime(2026, 5, 10, 14, 0, tzinfo=timezone.utc)
    attempt_repo = InMemoryProactiveAttemptRepository()
    decider = _FixedDecider(ProactiveDecision(True, "ok", "hi"))
    dispatcher, _, relationship_repo = await _pre_message_dispatcher(
        harness, decider=decider, attempts=attempt_repo,
    )
    await _permit_pre_message(relationship_repo, character.id)

    first = await dispatcher.evaluate(
        character_id=character.id, trigger=ProactiveTrigger.TICK, now=now,
    )
    assert first.outcome == ProactiveOutcome.SENT

    # Second push, a full interval later — still inside the cap.
    second = await dispatcher.evaluate(
        character_id=character.id,
        trigger=ProactiveTrigger.TICK,
        now=now + timedelta(days=2),
    )
    assert second.outcome == ProactiveOutcome.SENT

    # Third: the budget is spent, and days of extra silence do not refill it.
    third = await dispatcher.evaluate(
        character_id=character.id,
        trigger=ProactiveTrigger.TICK,
        now=now + timedelta(days=9),
    )
    assert third.outcome == ProactiveOutcome.GATE_BLOCKED
    assert third.reason.startswith(PRE_MESSAGE_CAP_REASON)
    assert f"/{PRE_MESSAGE_PROACTIVE_CAP}" in third.reason
    # Blocked in the cheap band: no model call was spent on the refusal.
    assert len(decider.calls) == 2


@pytest.mark.asyncio
async def test_pre_message_interval_floor_blocks_a_same_day_second_push(
) -> None:
    """One push a day at most while the player has said nothing."""
    harness = build_messaging_harness()
    character = await _enable_character(
        harness, idle_minutes=None, accepts_web_proactive=True,
    )
    now = datetime(2026, 5, 10, 14, 0, tzinfo=timezone.utc)
    attempt_repo = InMemoryProactiveAttemptRepository()
    await attempt_repo.add(
        _past_pre_message_send(character.id, now - timedelta(hours=3)),
    )
    decider = _FixedDecider(ProactiveDecision(True, "ok", "hi"))
    dispatcher, _, relationship_repo = await _pre_message_dispatcher(
        harness, decider=decider, attempts=attempt_repo,
    )
    await _permit_pre_message(relationship_repo, character.id)

    attempt = await dispatcher.evaluate(
        character_id=character.id, trigger=ProactiveTrigger.TICK, now=now,
    )

    assert attempt.outcome == ProactiveOutcome.GATE_BLOCKED
    assert attempt.reason.startswith(PRE_MESSAGE_INTERVAL_REASON)
    assert decider.calls == []


@pytest.mark.asyncio
async def test_pre_message_budget_stops_applying_once_the_player_speaks(
) -> None:
    """The cap is a cold-start ceiling, not a lifetime quota.

    Same character, same spent budget, same clock — the only difference
    is that the player has now said something, and the normal cadence
    (cooldown / daily limit / unanswered streak) takes over.
    """
    harness = build_messaging_harness()
    character = await _enable_character(
        harness, idle_minutes=None, accepts_web_proactive=True,
    )
    now = datetime(2026, 5, 10, 14, 0, tzinfo=timezone.utc)
    attempt_repo = InMemoryProactiveAttemptRepository()
    for index in range(PRE_MESSAGE_PROACTIVE_CAP):
        await attempt_repo.add(
            _past_pre_message_send(
                character.id, now - timedelta(days=5 + index),
            ),
        )
    decider = _FixedDecider(ProactiveDecision(True, "ok", "hi"))
    dispatcher, _, relationship_repo = await _pre_message_dispatcher(
        harness, decider=decider, attempts=attempt_repo,
    )
    await _permit_pre_message(relationship_repo, character.id)

    blocked = await dispatcher.evaluate(
        character_id=character.id, trigger=ProactiveTrigger.TICK, now=now,
    )
    assert blocked.outcome == ProactiveOutcome.GATE_BLOCKED
    assert blocked.reason.startswith(PRE_MESSAGE_CAP_REASON)

    # The player finally opens their mouth.
    spoke = character.update(
        name=None, summary=None, personality=None, interests=None,
        speaking_style=None, boundaries=None, aspirations=None,
        appearance=None,
        state=replace(
            character.state, last_active_at=now - timedelta(hours=2),
        ),
    )
    await harness.character_repository.save(spoke)

    attempt = await dispatcher.evaluate(
        character_id=character.id, trigger=ProactiveTrigger.TICK, now=now,
    )

    assert attempt.outcome == ProactiveOutcome.SENT
    assert len(decider.calls) == 1


@pytest.mark.asyncio
async def test_pre_message_budget_does_not_gate_promise_fulfilment() -> None:
    """PENDING_FOLLOW_UP is a promise being kept, not an unprompted push.

    It is already exempt from the first-user-message gate; the cap sits
    inside that same branch so the exemption has to survive verbatim.
    """
    harness = build_messaging_harness()
    character = await _enable_character(
        harness, idle_minutes=None, accepts_web_proactive=True,
    )
    now = datetime(2026, 5, 10, 14, 0, tzinfo=timezone.utc)
    attempt_repo = InMemoryProactiveAttemptRepository()
    for index in range(PRE_MESSAGE_PROACTIVE_CAP + 1):
        await attempt_repo.add(
            _past_pre_message_send(character.id, now - timedelta(minutes=index)),
        )
    decider = _FixedDecider(ProactiveDecision(True, "ok", "查到了"))
    dispatcher, _, relationship_repo = await _pre_message_dispatcher(
        harness, decider=decider, attempts=attempt_repo,
    )
    await _permit_pre_message(relationship_repo, character.id)

    attempt = await dispatcher.evaluate(
        character_id=character.id,
        trigger=ProactiveTrigger.PENDING_FOLLOW_UP,
        now=now,
    )

    assert attempt.outcome == ProactiveOutcome.SENT


@pytest.mark.asyncio
async def test_pre_message_budget_lookup_failure_blocks_rather_than_sends(
) -> None:
    """An unreadable audit log means an unknown budget — do not push."""
    harness = build_messaging_harness()
    character = await _enable_character(
        harness, idle_minutes=None, accepts_web_proactive=True,
    )

    class _BrokenAttempts(InMemoryProactiveAttemptRepository):
        async def list_recent_sent(self, character_id: str, *, limit: int = 8):
            raise RuntimeError("audit log unavailable")

    decider = _FixedDecider(ProactiveDecision(True, "ok", "hi"))
    dispatcher, _, relationship_repo = await _pre_message_dispatcher(
        harness, decider=decider, attempts=_BrokenAttempts(),
    )
    await _permit_pre_message(relationship_repo, character.id)

    attempt = await dispatcher.evaluate(
        character_id=character.id,
        trigger=ProactiveTrigger.TICK,
        now=datetime(2026, 5, 10, 14, 0, tzinfo=timezone.utc),
    )

    assert attempt.outcome == ProactiveOutcome.GATE_BLOCKED
    assert attempt.reason == PRE_MESSAGE_BUDGET_UNAVAILABLE_REASON
    assert decider.calls == []


async def _created_at(harness, character, when: datetime):
    """Give the character a creation stamp the delay window can read.

    The in-memory repository does not synthesise one (the column is
    server-managed in the SA adapter), so tests that care about the
    TR2-B window set it explicitly; every other test leaves it ``None``
    and the window stays out of the way.
    """
    stamped = replace(character, created_at=when)
    await harness.character_repository.save(stamped)
    return stamped


@pytest.mark.asyncio
async def test_pre_message_delay_window_blocks_a_freshly_created_character(
) -> None:
    """TR2-B: permission is pre-checked, so the first push still waits.

    Blocked in the cheap band and *before* the budget lookup — a refused
    tick must not spend a model call, and must not spend one of the two
    pre-message pushes either.
    """
    harness = build_messaging_harness()
    character = await _enable_character(
        harness, idle_minutes=None, accepts_web_proactive=True,
    )
    now = datetime(2026, 5, 10, 14, 0, tzinfo=timezone.utc)
    await _created_at(
        harness,
        character,
        now - timedelta(hours=PRE_MESSAGE_PROACTIVE_DELAY_HOURS - 2),
    )
    attempt_repo = InMemoryProactiveAttemptRepository()
    decider = _FixedDecider(ProactiveDecision(True, "ok", "hi"))
    dispatcher, _, relationship_repo = await _pre_message_dispatcher(
        harness, decider=decider, attempts=attempt_repo,
    )
    await _permit_pre_message(relationship_repo, character.id)

    attempt = await dispatcher.evaluate(
        character_id=character.id, trigger=ProactiveTrigger.TICK, now=now,
    )

    assert attempt.outcome == ProactiveOutcome.GATE_BLOCKED
    assert attempt.reason.startswith(PRE_MESSAGE_DELAY_REASON)
    assert decider.calls == []
    assert await attempt_repo.list_recent_sent(character.id, limit=5) == []


@pytest.mark.asyncio
async def test_pre_message_delay_window_lapses_and_the_push_goes_out() -> None:
    """Same character, same permission — only the clock moved."""
    harness = build_messaging_harness()
    character = await _enable_character(
        harness, idle_minutes=None, accepts_web_proactive=True,
    )
    created = datetime(2026, 5, 10, 14, 0, tzinfo=timezone.utc)
    await _created_at(harness, character, created)
    decider = _FixedDecider(ProactiveDecision(True, "ok", "hi"))
    dispatcher, _, relationship_repo = await _pre_message_dispatcher(
        harness, decider=decider,
    )
    await _permit_pre_message(relationship_repo, character.id)

    attempt = await dispatcher.evaluate(
        character_id=character.id,
        trigger=ProactiveTrigger.TICK,
        now=created + timedelta(hours=PRE_MESSAGE_PROACTIVE_DELAY_HOURS),
    )

    assert attempt.outcome == ProactiveOutcome.SENT


@pytest.mark.asyncio
async def test_pre_message_delay_window_does_not_refund_the_cap() -> None:
    """The two floors stack; neither one resets the other.

    Waiting out the delay window does not buy back a spent budget, and
    an untouched budget does not shorten the window.
    """
    harness = build_messaging_harness()
    character = await _enable_character(
        harness, idle_minutes=None, accepts_web_proactive=True,
    )
    created = datetime(2026, 5, 10, 14, 0, tzinfo=timezone.utc)
    await _created_at(harness, character, created)
    attempt_repo = InMemoryProactiveAttemptRepository()
    for index in range(PRE_MESSAGE_PROACTIVE_CAP):
        await attempt_repo.add(
            _past_pre_message_send(
                character.id, created - timedelta(days=index),
            ),
        )
    decider = _FixedDecider(ProactiveDecision(True, "ok", "hi"))
    dispatcher, _, relationship_repo = await _pre_message_dispatcher(
        harness, decider=decider, attempts=attempt_repo,
    )
    await _permit_pre_message(relationship_repo, character.id)

    inside = await dispatcher.evaluate(
        character_id=character.id,
        trigger=ProactiveTrigger.TICK,
        now=created + timedelta(hours=1),
    )
    assert inside.reason.startswith(PRE_MESSAGE_DELAY_REASON)

    outside = await dispatcher.evaluate(
        character_id=character.id,
        trigger=ProactiveTrigger.TICK,
        now=created + timedelta(days=4),
    )
    assert outside.outcome == ProactiveOutcome.GATE_BLOCKED
    assert outside.reason.startswith(PRE_MESSAGE_CAP_REASON)
    assert decider.calls == []


@pytest.mark.asyncio
async def test_pre_message_delay_window_ignores_a_character_who_was_spoken_to(
) -> None:
    """A player who chats straight away is not made to wait.

    The window is a floor under *unprompted* first contact only; once
    there is a conversation the normal cadence owns the rhythm, so a
    brand-new character may speak the same hour it was created.
    """
    harness = build_messaging_harness()
    character = await _enable_character(
        harness, idle_minutes=90.0, accepts_web_proactive=True,
    )
    now = datetime.now(timezone.utc)
    await _created_at(harness, character, now - timedelta(minutes=95))
    decider = _FixedDecider(ProactiveDecision(True, "ok", "hi"))
    dispatcher, _, relationship_repo = await _pre_message_dispatcher(
        harness, decider=decider,
    )
    await _permit_pre_message(relationship_repo, character.id)

    attempt = await dispatcher.evaluate(
        character_id=character.id, trigger=ProactiveTrigger.TICK, now=now,
    )

    assert attempt.outcome == ProactiveOutcome.SENT


@pytest.mark.asyncio
async def test_pre_message_delay_window_does_not_gate_promise_fulfilment(
) -> None:
    """A kept promise is not unprompted contact — the exemption holds."""
    harness = build_messaging_harness()
    character = await _enable_character(
        harness, idle_minutes=None, accepts_web_proactive=True,
    )
    now = datetime(2026, 5, 10, 14, 0, tzinfo=timezone.utc)
    await _created_at(harness, character, now - timedelta(minutes=5))
    decider = _FixedDecider(ProactiveDecision(True, "ok", "查到了"))
    dispatcher, _, relationship_repo = await _pre_message_dispatcher(
        harness, decider=decider,
    )
    await _permit_pre_message(relationship_repo, character.id)

    attempt = await dispatcher.evaluate(
        character_id=character.id,
        trigger=ProactiveTrigger.PENDING_FOLLOW_UP,
        now=now,
    )

    assert attempt.outcome == ProactiveOutcome.SENT


@pytest.mark.asyncio
async def test_pre_message_delay_window_stays_behind_the_permission_question(
) -> None:
    """Seed permission is still asked first, not the clock.

    Pins the ordering so the audit trail does not start blaming the
    delay window for what is actually a missing opt-in.
    """
    harness = build_messaging_harness()
    character = await _enable_character(
        harness, idle_minutes=None, accepts_web_proactive=True,
    )
    now = datetime(2026, 5, 10, 14, 0, tzinfo=timezone.utc)
    await _created_at(harness, character, now - timedelta(minutes=5))
    decider = _FixedDecider(ProactiveDecision(True, "ok", "hi"))
    dispatcher, _, _ = await _pre_message_dispatcher(harness, decider=decider)

    attempt = await dispatcher.evaluate(
        character_id=character.id, trigger=ProactiveTrigger.TICK, now=now,
    )

    assert attempt.outcome == ProactiveOutcome.GATE_BLOCKED
    assert attempt.reason == "waiting for first user message"


@pytest.mark.asyncio
async def test_legacy_user_message_unblocks_when_last_active_missing() -> None:
    harness = build_messaging_harness()
    character = await _enable_character(
        harness, idle_minutes=None, accepts_web_proactive=True,
    )
    conversation = Conversation.start(character_id=character.id)
    await harness.conversation_repository.save(
        conversation.append(Message(role=MessageRole.USER, content="你好")),
    )
    decider = _FixedDecider(ProactiveDecision(True, "ok", "hi"))
    dispatcher, _ = _make_dispatcher(harness, decider=decider)

    attempt = await dispatcher.evaluate(
        character_id=character.id, trigger=ProactiveTrigger.TICK,
    )

    assert attempt.outcome == ProactiveOutcome.SENT
    assert len(decider.calls) == 1


@pytest.mark.asyncio
async def test_disabled_character_short_circuits() -> None:
    harness = build_messaging_harness()
    dto = await create_character(harness, proactive_enabled=False)
    dispatcher, attempts = _make_dispatcher(
        harness, decider=_FixedDecider(ProactiveDecision(True, "ok", "hi")),
    )

    attempt = await dispatcher.evaluate(
        character_id=dto.id, trigger=ProactiveTrigger.TICK,
    )

    assert attempt.outcome == ProactiveOutcome.DISABLED
    assert harness.telegram_adapter.sent == []


@pytest.mark.asyncio
async def test_gate_blocks_when_user_just_spoke() -> None:
    harness = build_messaging_harness()
    character = await _enable_character(harness, idle_minutes=1.0)
    dispatcher, _ = _make_dispatcher(
        harness, decider=_FixedDecider(ProactiveDecision(True, "ok", "hi")),
    )

    attempt = await dispatcher.evaluate(
        character_id=character.id, trigger=ProactiveTrigger.TICK,
    )

    assert attempt.outcome == ProactiveOutcome.GATE_BLOCKED
    assert "idle" in attempt.reason


@pytest.mark.asyncio
async def test_no_binding_marks_attempt() -> None:
    harness = build_messaging_harness()
    character = await _enable_character(harness)
    # Account exists but no binding has accepts_proactive=True.
    account = await create_telegram_account(harness, character_id=character.id)
    from kokoro_link.domain.entities.channel_binding import ChannelBinding

    binding = ChannelBinding.create(account_id=account.id, chat_ref="c1")
    await harness.binding_repository.save(binding)

    dispatcher, _ = _make_dispatcher(
        harness, decider=_FixedDecider(ProactiveDecision(True, "ok", "hi")),
    )

    attempt = await dispatcher.evaluate(
        character_id=character.id, trigger=ProactiveTrigger.TICK,
    )

    assert attempt.outcome == ProactiveOutcome.NO_BINDING
    assert harness.telegram_adapter.sent == []


@pytest.mark.asyncio
async def test_decider_skipping_logs_reason() -> None:
    harness = build_messaging_harness()
    character = await _enable_character(harness)
    account = await create_telegram_account(harness, character_id=character.id)
    from kokoro_link.domain.entities.channel_binding import ChannelBinding

    binding = ChannelBinding.create(
        account_id=account.id, chat_ref="c1", accepts_proactive=True,
    )
    await harness.binding_repository.save(binding)

    dispatcher, _ = _make_dispatcher(
        harness,
        decider=_FixedDecider(
            ProactiveDecision(False, "not in the mood", None),
        ),
    )

    attempt = await dispatcher.evaluate(
        character_id=character.id, trigger=ProactiveTrigger.TICK,
    )

    assert attempt.outcome == ProactiveOutcome.DECIDER_SKIPPED
    assert attempt.reason == "not in the mood"
    assert harness.telegram_adapter.sent == []


@pytest.mark.asyncio
async def test_sent_path_appends_message_and_pushes() -> None:
    harness = build_messaging_harness()
    character = await _enable_character(harness)
    account = await create_telegram_account(harness, character_id=character.id)
    from kokoro_link.domain.entities.channel_binding import ChannelBinding

    binding = ChannelBinding.create(
        account_id=account.id, chat_ref="c1", accepts_proactive=True,
    )
    await harness.binding_repository.save(binding)

    dispatcher, _ = _make_dispatcher(
        harness,
        decider=_FixedDecider(
            ProactiveDecision(True, "想跟你打招呼", "嗨，我剛做完練習"),
        ),
    )

    attempt = await dispatcher.evaluate(
        character_id=character.id, trigger=ProactiveTrigger.TICK,
    )

    assert attempt.outcome == ProactiveOutcome.SENT
    assert attempt.message == "嗨，我剛做完練習"
    assert attempt.binding_id == binding.id
    assert len(harness.telegram_adapter.sent) == 1
    sent = harness.telegram_adapter.sent[0]
    assert sent.text == "嗨，我剛做完練習"
    assert sent.credentials == account.credentials
    # Proactive sends have no inbound event to reply to — the outbound
    # must carry no reply affinity so channel adapters keep using push.
    assert sent.reply_context == {}

    # conversation now has an assistant message persisted
    refreshed = await harness.binding_repository.get(binding.id)
    assert refreshed is not None and refreshed.conversation_id is not None
    convo = await harness.conversation_repository.get(refreshed.conversation_id)
    assert convo is not None
    assert convo.messages[-1].content == "嗨，我剛做完練習"


@pytest.mark.asyncio
async def test_pre_composed_external_push_sends_segments_with_attachment_last() -> None:
    harness = build_messaging_harness()
    character = await _enable_character(harness)
    account = await create_telegram_account(harness, character_id=character.id)
    from kokoro_link.domain.entities.channel_binding import ChannelBinding

    binding = ChannelBinding.create(
        account_id=account.id, chat_ref="c1", accepts_proactive=True,
    )
    await harness.binding_repository.save(binding)
    dispatcher, _ = _make_dispatcher(
        harness,
        decider=_FixedDecider(ProactiveDecision(True, "ok", "unused")),
    )
    attachment = OutboundAttachment(
        kind="image",
        url="https://cdn.example.test/proactive.png",
        mime_type="image/png",
    )

    attempt = await dispatcher.deliver_pre_composed(
        character_id=character.id,
        text="第一則\n\n*拿起手機* 第二則",
        trigger=ProactiveTrigger.PENDING_FOLLOW_UP,
        attachments=(attachment,),
    )

    assert attempt.outcome == ProactiveOutcome.SENT
    assert attempt.binding_id == binding.id
    assert [message.text for message in harness.telegram_adapter.sent] == [
        "第一則",
        "第二則",
    ]
    assert harness.telegram_adapter.sent[0].attachments == ()
    assert harness.telegram_adapter.sent[1].attachments == (attachment,)


@pytest.mark.asyncio
async def test_gate_blocked_attempts_do_not_reset_cooldown() -> None:
    """Regression: earlier versions anchored cooldown on "last attempt"
    which included gate-blocked attempts. Every 5-minute tick would
    write a GATE_BLOCKED row and push the cooldown timer forward, so a
    30-minute cooldown effectively never lapsed. Cooldown must be
    anchored on attempts that *actually* cost LLM budget."""
    harness = build_messaging_harness()
    dto = await create_character(harness)
    character = await harness.character_repository.get(dto.id)
    assert character is not None

    # Enable proactive but mark the user as having just spoken so the
    # idle gate trips on every tick.
    just_spoke = character.update(
        name=None, summary=None, personality=None, interests=None,
        speaking_style=None, boundaries=None, aspirations=None, appearance=None,
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
            last_active_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        ),
        proactive_enabled=True,
        proactive_cooldown_minutes=30,
    )
    await harness.character_repository.save(just_spoke)

    dispatcher, attempts = _make_dispatcher(
        harness, decider=_FixedDecider(ProactiveDecision(True, "ok", "hi")),
    )

    # First tick: idle gate blocks (user spoke 1 min ago < 10 min threshold)
    await dispatcher.evaluate(
        character_id=character.id, trigger=ProactiveTrigger.TICK,
    )
    # 5 min later, still within idle window → blocks again
    later = datetime.now(timezone.utc) + timedelta(minutes=5)
    attempt = await dispatcher.evaluate(
        character_id=character.id, trigger=ProactiveTrigger.TICK, now=later,
    )

    assert attempt.outcome == ProactiveOutcome.GATE_BLOCKED
    # Reason must come from the *idle* check, not a cooldown reset by
    # the previous GATE_BLOCKED row. If the cooldown anchor regresses,
    # this reason becomes "cooldown active ..." and the test fails.
    assert "idle" in attempt.reason


@pytest.mark.asyncio
async def test_daily_limit_resets_at_local_midnight_not_utc() -> None:
    """Regression: the daily-limit ``count_sent_today`` uses
    ``now.replace(hour=0, ...)``. Dispatcher must hand it a *local*
    timezone datetime so the reset line matches the operator's
    intuition (midnight in their TZ), not UTC midnight.

    Scenario (GMT+8): an attempt at UTC 22:00 yesterday is local
    06:00 today — still in the same local day. A UTC-anchored check
    would think it was yesterday and undercount.
    """
    from datetime import timezone as dtz

    from datetime import timezone as dtz

    harness = build_messaging_harness()
    gmt8 = dtz(timedelta(hours=8))
    # Today local 10:00 == today UTC 02:00
    today_local_1000_as_utc = datetime(2026, 4, 18, 2, 0, tzinfo=timezone.utc)
    # Yesterday UTC 22:00 == today local 06:00
    yesterday_utc_2200 = datetime(2026, 4, 17, 22, 0, tzinfo=timezone.utc)

    # Pin last_active_at well before our synthetic "now" so the idle
    # gate doesn't trip this test; we only want to exercise daily-limit.
    dto = await create_character(harness)
    base = await harness.character_repository.get(dto.id)
    assert base is not None
    character = base.update(
        name=None, summary=None, personality=None, interests=None,
        speaking_style=None, boundaries=None, aspirations=None, appearance=None,
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
            last_active_at=today_local_1000_as_utc - timedelta(hours=6),
        ),
        proactive_enabled=True,
    )
    await harness.character_repository.save(character)

    attempts = InMemoryProactiveAttemptRepository()
    from kokoro_link.domain.entities.proactive_attempt import ProactiveAttempt

    # Seed three SENT rows stamped at the local-today morning (they are
    # "today" locally, even though UTC 22:00 yesterday looks like the
    # prior date in UTC).
    for i in range(3):
        await attempts.add(
            ProactiveAttempt.record(
                character_id=character.id,
                trigger=ProactiveTrigger.TICK,
                outcome=ProactiveOutcome.SENT,
                reason=f"seed {i}",
                now=yesterday_utc_2200 + timedelta(minutes=i),
            ),
        )

    dispatcher = ProactiveDispatcher(
        character_repository=harness.character_repository,
        conversation_repository=harness.conversation_repository,
        account_repository=harness.account_repository,
        binding_repository=harness.binding_repository,
        attempt_repository=attempts,
        gate=HeuristicProactiveGate(local_tz=timezone.utc, quiet_hour_start=0, quiet_hour_end=0),
        decider=_FixedDecider(ProactiveDecision(True, "ok", "hi")),
        adapters={
            Platform.TELEGRAM: harness.telegram_adapter,
            Platform.LINE: harness.line_adapter,
        },
        local_tz=gmt8,
    )

    attempt = await dispatcher.evaluate(
        character_id=character.id,
        trigger=ProactiveTrigger.TICK,
        now=today_local_1000_as_utc,
    )

    # With local_tz=GMT+8, "today" runs from local 00:00 =
    # UTC 16:00 yesterday → those seeded sends at UTC 22:00 yesterday
    # (local 06:00 today) count, pushing sent_today to 3/3 → blocked.
    assert attempt.outcome == ProactiveOutcome.GATE_BLOCKED
    assert "daily limit" in attempt.reason


@pytest.mark.asyncio
async def test_daily_limit_uses_owner_timezone_not_dispatcher_fallback() -> None:
    harness = build_messaging_harness()
    owner_tz = ZoneInfo("Asia/Taipei")
    now = datetime(2026, 6, 14, 16, 30, tzinfo=timezone.utc)
    earlier_same_owner_day = datetime(2026, 6, 14, 16, 5, tzinfo=timezone.utc)

    dto = await create_character(harness)
    base = await harness.character_repository.get(dto.id)
    assert base is not None
    character = replace(
        base,
        user_id="owner-tw",
        proactive_enabled=True,
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
            last_active_at=now - timedelta(hours=6),
        ),
    )
    await harness.character_repository.save(character)

    attempts = InMemoryProactiveAttemptRepository()
    from kokoro_link.domain.entities.proactive_attempt import ProactiveAttempt

    for idx in range(3):
        await attempts.add(ProactiveAttempt.record(
            character_id=character.id,
            trigger=ProactiveTrigger.TICK,
            outcome=ProactiveOutcome.SENT,
            reason=f"seed {idx}",
            now=earlier_same_owner_day + timedelta(minutes=idx),
        ))

    dispatcher = ProactiveDispatcher(
        character_repository=harness.character_repository,
        conversation_repository=harness.conversation_repository,
        account_repository=harness.account_repository,
        binding_repository=harness.binding_repository,
        attempt_repository=attempts,
        gate=HeuristicProactiveGate(
            local_tz=timezone.utc, quiet_hour_start=0, quiet_hour_end=0,
        ),
        decider=_FixedDecider(ProactiveDecision(True, "ok", "hi")),
        adapters={
            Platform.TELEGRAM: harness.telegram_adapter,
            Platform.LINE: harness.line_adapter,
        },
        local_tz=timezone.utc,
        operator_profile_service=_OperatorProfileService("Asia/Taipei"),
    )

    attempt = await dispatcher.evaluate(
        character_id=character.id,
        trigger=ProactiveTrigger.TICK,
        now=now,
    )

    assert now.astimezone(owner_tz).date().isoformat() == "2026-06-15"
    assert attempt.outcome == ProactiveOutcome.GATE_BLOCKED
    assert "daily limit" in attempt.reason


@pytest.mark.asyncio
async def test_night_hours_gate_uses_owner_timezone_not_dispatcher_fallback() -> None:
    harness = build_messaging_harness()
    now = datetime(2026, 6, 14, 16, 30, tzinfo=timezone.utc)

    dto = await create_character(harness)
    base = await harness.character_repository.get(dto.id)
    assert base is not None
    character = replace(
        base,
        user_id="owner-tw",
        proactive_enabled=True,
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
            last_active_at=now - timedelta(hours=6),
        ),
    )
    await harness.character_repository.save(character)

    dispatcher = ProactiveDispatcher(
        character_repository=harness.character_repository,
        conversation_repository=harness.conversation_repository,
        account_repository=harness.account_repository,
        binding_repository=harness.binding_repository,
        attempt_repository=InMemoryProactiveAttemptRepository(),
        gate=HeuristicProactiveGate(
            local_tz=timezone.utc, quiet_hour_start=0, quiet_hour_end=7,
        ),
        decider=_FixedDecider(ProactiveDecision(True, "ok", "hi")),
        adapters={
            Platform.TELEGRAM: harness.telegram_adapter,
            Platform.LINE: harness.line_adapter,
        },
        local_tz=timezone.utc,
        operator_profile_service=_OperatorProfileService("Asia/Taipei"),
    )

    attempt = await dispatcher.evaluate(
        character_id=character.id,
        trigger=ProactiveTrigger.TICK,
        now=now,
    )

    assert attempt.outcome == ProactiveOutcome.GATE_BLOCKED
    assert "night-hours" in attempt.reason
    assert "00:xx local" in attempt.reason


@pytest.mark.asyncio
async def test_daily_limit_blocks_after_sent_today() -> None:
    harness = build_messaging_harness()
    character = await _enable_character(harness)
    account = await create_telegram_account(harness, character_id=character.id)
    from kokoro_link.domain.entities.channel_binding import ChannelBinding

    binding = ChannelBinding.create(
        account_id=account.id, chat_ref="c1", accepts_proactive=True,
    )
    await harness.binding_repository.save(binding)

    # daily_limit default is 3; push three SENT attempts so the 4th hits the gate
    attempts = InMemoryProactiveAttemptRepository()
    from kokoro_link.domain.entities.proactive_attempt import ProactiveAttempt

    eval_now = datetime.now(timezone.utc) + timedelta(hours=5)
    for idx in range(3):
        await attempts.add(
            ProactiveAttempt.record(
                character_id=character.id,
                trigger=ProactiveTrigger.TICK,
                outcome=ProactiveOutcome.SENT,
                reason=f"seeded {idx}",
                now=eval_now - timedelta(hours=1, minutes=idx),
            ),
        )

    dispatcher, _ = _make_dispatcher(
        harness,
        decider=_FixedDecider(ProactiveDecision(True, "ok", "hi")),
        attempts=attempts,
    )

    # Bypass cooldown by using a faraway "now"
    attempt = await dispatcher.evaluate(
        character_id=character.id,
        trigger=ProactiveTrigger.TICK,
        now=eval_now,
    )

    assert attempt.outcome == ProactiveOutcome.GATE_BLOCKED


@pytest.mark.asyncio
async def test_intention_judge_skip_prevents_message_composition() -> None:
    harness = build_messaging_harness()
    character = await _enable_character(harness, accepts_web_proactive=True)
    decider = _FixedDecider(ProactiveDecision(True, "ok", "hi"))
    judge = _FixedIntentionJudge(
        ProactiveIntentionDecision(
            should_consume_slot=False,
            reason="只是天氣素材，沒有真正想說的事",
            risk="像無意義推播",
            best_timing="evening",
        ),
    )
    dispatcher, attempts = _make_dispatcher(
        harness,
        decider=decider,
        intention_judge=judge,
    )

    attempt = await dispatcher.evaluate(
        character_id=character.id,
        trigger=ProactiveTrigger.TICK,
    )

    assert attempt.outcome == ProactiveOutcome.INTENTION_SKIPPED
    assert "intention skipped" in attempt.reason
    assert "best_timing=evening" in attempt.reason
    assert decider.calls == []
    assert len(judge.calls) == 1
    assert await attempts.count_sent_today(
        character.id, now=datetime.now(timezone.utc),
    ) == 0


@pytest.mark.asyncio
async def test_proactive_outcomes_emit_emotion_events_and_link_turn_refs() -> None:
    harness = build_messaging_harness()
    character = await _enable_character(harness, accepts_web_proactive=True)
    emotion_events = InMemoryEmotionEventRepository()
    turn_records = InMemoryTurnRecordRepository()
    turn_recorder = BackgroundTurnRecorder(turn_records)
    dispatcher, _ = _make_dispatcher(
        harness,
        decider=_FixedDecider(ProactiveDecision(False, "unused", None)),
        turn_recorder=turn_recorder,
        emotion_event_repository=emotion_events,
        prompt_pack_hash_provider=lambda: "proactive-pack-with-flags",
    )

    outcomes = (
        ProactiveOutcome.DISABLED,
        ProactiveOutcome.GATE_BLOCKED,
        ProactiveOutcome.NO_BINDING,
        ProactiveOutcome.INTENTION_SKIPPED,
        ProactiveOutcome.DECIDER_SKIPPED,
        ProactiveOutcome.SENT,
        ProactiveOutcome.ERRORED,
    )
    attempts = []
    for outcome in outcomes:
        attempts.append(await dispatcher._log(  # noqa: SLF001 - unit covers audit hook
            character_id=character.id,
            trigger=ProactiveTrigger.TICK,
            outcome=outcome,
            reason=f"reason {outcome.value}",
            now=datetime.now(timezone.utc),
            message="hi" if outcome == ProactiveOutcome.SENT else None,
        ))
    await turn_recorder.flush()

    events = await emotion_events.list_recent(
        character_id=character.id,
        operator_id="default",
        since=datetime(2020, 1, 1, tzinfo=timezone.utc),
        limit=20,
    )
    assert {e.cause_ref_id for e in events} == {a.id for a in attempts}
    assert {e.emotion_label for e in events} == {
        f"proactive:{outcome.value}" for outcome in outcomes
    }

    records = await turn_records.list_recent(character_id=character.id)
    assert len(records) == len(outcomes)
    for record in records:
        assert record.prompt_pack_hash == "proactive-pack-with-flags"
        assert record.post_turn_refs["proactive_attempt_id"]
        assert len(record.post_turn_refs["emotion_event_ids"]) == 1


@pytest.mark.asyncio
async def test_sent_turn_record_persists_the_assembled_decider_prompt() -> None:
    """A message that actually went out must be auditable.

    Without this the operator can see *what* the character said but not
    *what it was looking at* when it decided to say it (COMMITMENT_
    LIFECYCLE_AND_FRESHNESS_PLAN §2 P0).
    """
    harness = build_messaging_harness()
    character = await _enable_character(harness)
    account = await create_telegram_account(harness, character_id=character.id)
    from kokoro_link.domain.entities.channel_binding import ChannelBinding

    binding = ChannelBinding.create(
        account_id=account.id, chat_ref="c1", accepts_proactive=True,
    )
    await harness.binding_repository.save(binding)

    assembled = "你是角色「木木」，正在考慮是否主動向使用者傳訊息。\n【接下來幾天的行程】…"
    turn_records = InMemoryTurnRecordRepository()
    turn_recorder = BackgroundTurnRecorder(turn_records)
    dispatcher, _ = _make_dispatcher(
        harness,
        decider=_FixedDecider(
            ProactiveDecision(
                True,
                "想跟你打招呼",
                "嗨，我剛做完練習",
                prompt_assembled=assembled,
            ),
        ),
        turn_recorder=turn_recorder,
    )

    attempt = await dispatcher.evaluate(
        character_id=character.id, trigger=ProactiveTrigger.TICK,
    )
    await turn_recorder.flush()

    assert attempt.outcome == ProactiveOutcome.SENT
    records = await turn_records.list_recent(character_id=character.id)
    assert len(records) == 1
    assert records[0].prompt_assembled == assembled


@pytest.mark.asyncio
async def test_skipped_and_gate_blocked_ticks_store_no_prompt() -> None:
    """Ticks that produced no message must not carry the prompt.

    A ~5-minute tick cadence means hundreds of skip/blocked rows a day
    per character; persisting a ~17k-char prompt on each would bloat
    ``turn_records`` for zero audit value.
    """
    harness = build_messaging_harness()
    character = await _enable_character(harness)
    account = await create_telegram_account(harness, character_id=character.id)
    from kokoro_link.domain.entities.channel_binding import ChannelBinding

    binding = ChannelBinding.create(
        account_id=account.id, chat_ref="c1", accepts_proactive=True,
    )
    await harness.binding_repository.save(binding)

    turn_records = InMemoryTurnRecordRepository()
    turn_recorder = BackgroundTurnRecorder(turn_records)
    dispatcher, _ = _make_dispatcher(
        harness,
        decider=_FixedDecider(
            ProactiveDecision(
                False,
                "現在不想打擾",
                None,
                prompt_assembled="（skip 也組過 prompt，但不該落庫）",
            ),
        ),
        turn_recorder=turn_recorder,
    )

    skipped = await dispatcher.evaluate(
        character_id=character.id, trigger=ProactiveTrigger.TICK,
    )
    blocked = await dispatcher.evaluate(
        character_id=character.id, trigger=ProactiveTrigger.TICK,
    )
    await turn_recorder.flush()

    assert skipped.outcome == ProactiveOutcome.DECIDER_SKIPPED
    assert blocked.outcome == ProactiveOutcome.GATE_BLOCKED
    records = await turn_records.list_recent(character_id=character.id)
    assert len(records) == 2
    assert [r.prompt_assembled for r in records] == ["", ""]


# ---------------------------------------------------------------------------
# Web-channel proactive delivery (no TG/LINE binding required)
# ---------------------------------------------------------------------------


def _make_dispatcher_with_bus(harness, *, decider):
    """Dispatcher with an attached ``ProactiveEventBus`` so tests can
    observe publish calls. Returned alongside the bus + attempts repo."""
    from kokoro_link.application.services.proactive_event_bus import (
        ProactiveEventBus,
    )

    attempt_repo = InMemoryProactiveAttemptRepository()
    bus = ProactiveEventBus()
    dispatcher = ProactiveDispatcher(
        character_repository=harness.character_repository,
        conversation_repository=harness.conversation_repository,
        account_repository=harness.account_repository,
        binding_repository=harness.binding_repository,
        attempt_repository=attempt_repo,
        gate=HeuristicProactiveGate(local_tz=timezone.utc, quiet_hour_start=0, quiet_hour_end=0),
        decider=decider,
        adapters={
            Platform.TELEGRAM: harness.telegram_adapter,
            Platform.LINE: harness.line_adapter,
        },
        event_bus=bus,
    )
    return dispatcher, bus, attempt_repo


@pytest.mark.asyncio
async def test_web_delivery_creates_conversation_and_publishes_event() -> None:
    """Character with ``accepts_web_proactive=True`` but no TG/LINE
    binding should still fire: dispatcher writes to the web thread,
    bumps the unread badge, and publishes a bus event."""
    harness = build_messaging_harness()
    character = await _enable_character(harness, accepts_web_proactive=True)

    dispatcher, bus, _ = _make_dispatcher_with_bus(
        harness,
        decider=_FixedDecider(ProactiveDecision(True, "web only", "hi there")),
    )

    async with bus.subscription() as queue:
        attempt = await dispatcher.evaluate(
            character_id=character.id, trigger=ProactiveTrigger.TICK,
        )

        assert attempt.outcome == ProactiveOutcome.SENT
        # No binding was configured, so binding_id stays None even on SENT.
        assert attempt.binding_id is None

        # Badge counter incremented on the persisted character row.
        stored = await harness.character_repository.get(character.id)
        assert stored is not None
        assert stored.unread_proactive_count == 1

        # A web conversation exists and holds the assistant message.
        latest = await harness.conversation_repository.latest_for_character(
            character.id, source="web",
        )
        assert latest is not None
        assert latest.source == "web"
        assert latest.messages[-1].content == "hi there"

        # Event was published exactly once with the final counter.
        event = queue.get_nowait()
        assert event.character_id == character.id
        assert event.unread_count == 1
        assert event.message == "hi there"


@pytest.mark.asyncio
async def test_web_opt_out_with_no_binding_still_reports_no_binding() -> None:
    """``accepts_web_proactive=False`` + no messaging binding means
    there is genuinely nowhere to push. Attempt must log NO_BINDING."""
    harness = build_messaging_harness()
    character = await _enable_character(harness, accepts_web_proactive=False)

    dispatcher, bus, _ = _make_dispatcher_with_bus(
        harness,
        decider=_FixedDecider(ProactiveDecision(True, "ok", "nope")),
    )

    attempt = await dispatcher.evaluate(
        character_id=character.id, trigger=ProactiveTrigger.TICK,
    )

    assert attempt.outcome == ProactiveOutcome.NO_BINDING
    stored = await harness.character_repository.get(character.id)
    assert stored is not None
    assert stored.unread_proactive_count == 0
    assert bus.subscriber_count() == 0
