"""End-to-end BDD for the proactive dispatcher pipeline.

The in-memory harness gives us a fully-wired dispatcher, fake adapters,
and real heuristic gate. We stub the decider per-test so we can steer
between SENT / SKIPPED paths without involving an LLM.
"""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

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
async def test_pre_message_proactive_permission_without_cadence_still_waits() -> None:
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
    )

    assert attempt.outcome == ProactiveOutcome.GATE_BLOCKED
    assert attempt.reason == "waiting for first user message"
    assert decider.calls == []


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
