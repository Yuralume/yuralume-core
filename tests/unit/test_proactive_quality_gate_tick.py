"""FA — the output-quality withhold, driven through ``evaluate`` itself.

The rest of the proactive QG tests reach in through ``__new__`` and call
``_gate_proactive_decision`` directly. That pins the band's disposal but
says nothing about what a *tick* does with it, and everything this file
covers lives outside that method: which audit outcome is written, whether
the claimed event seed flows back, whether the daily quota moved, and —
the one that bit — whether the next tick is still allowed to happen.

The cooldown is the point. ``DECIDER_SKIPPED`` anchors it (the character
chose silence and that choice is meant to hold), so tagging a quality
withhold with the same outcome meant one broken draft silenced the whole
window even though the player received nothing at all.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from kokoro_link.application.services.output_quality import (
    OUTCOME_HARD_SKIPPED,
    OutputQualityOrchestrator,
)
from kokoro_link.application.services.proactive_dispatcher import (
    ProactiveDispatcher,
)
from kokoro_link.contracts.novelty_gate import NoveltyGateContext, NoveltyVerdict
from kokoro_link.contracts.proactive import (
    ProactiveContext,
    ProactiveDecision,
    ProactiveDeciderPort,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.platform import Platform
from kokoro_link.domain.value_objects.proactive_outcome import ProactiveOutcome
from kokoro_link.domain.value_objects.proactive_trigger import ProactiveTrigger
from kokoro_link.infrastructure.proactive.heuristic_gate import (
    HeuristicProactiveGate,
)
from kokoro_link.infrastructure.repositories.in_memory_proactive_attempts import (
    InMemoryProactiveAttemptRepository,
)
from tests.unit._messaging_harness import build_messaging_harness, create_character

NOW = datetime(2026, 8, 26, 15, 0, tzinfo=timezone.utc)
LEAKY = '早安！{"should_send": true, "message": "早安"}'
CLEAN = "早安，今天想跟你說個小事"


# --- doubles ---------------------------------------------------------


class _ScriptedGate:
    """Output-quality judge answering from a queue; last answer repeats."""

    def __init__(self, *verdicts: NoveltyVerdict) -> None:
        self._verdicts = list(verdicts)
        self.calls: list[NoveltyGateContext] = []

    async def evaluate(self, context: NoveltyGateContext, *, character=None):  # noqa: ANN001
        del character
        self.calls.append(context)
        index = min(len(self.calls) - 1, len(self._verdicts) - 1)
        return self._verdicts[index]


class _ScriptedDecider(ProactiveDeciderPort):
    """One decision per call; the last one repeats."""

    def __init__(self, *decisions: ProactiveDecision) -> None:
        self._decisions = list(decisions)
        self.calls: list[ProactiveContext] = []

    async def decide(self, context: ProactiveContext) -> ProactiveDecision:
        self.calls.append(context)
        index = min(len(self.calls) - 1, len(self._decisions) - 1)
        return self._decisions[index]


class _StubSeedDispenser:
    """Hands out one seed and records whether it was ever handed back.

    Duck-typed rather than a real ``EventSeedDispenser``: the dispatcher
    only reads the claimed event's text and the inbox row's id, and a real
    one would drag two repositories in for a fact this file states in a
    line.
    """

    def __init__(self) -> None:
        self.released: list[tuple[str, str]] = []

    async def claim(self, *, character_id, surface, now=None):  # noqa: ANN001
        del character_id, surface, now
        return SimpleNamespace(
            item=SimpleNamespace(id="inbox-1"),
            event=SimpleNamespace(
                id="event-1",
                title="路口的貓又出現了",
                summary="早上七點有人拍到牠。",
                source="local",
                locale="zh-TW",
            ),
        )

    async def release(self, *, item_id: str, surface: str) -> bool:
        self.released.append((item_id, surface))
        return True


# --- harness ---------------------------------------------------------


async def _web_character(harness) -> Character:
    dto = await create_character(harness)
    character = await harness.character_repository.get(dto.id)
    assert character is not None
    updated = character.update(
        name=None, summary=None, personality=None, interests=None,
        speaking_style=None, boundaries=None, aspirations=None,
        appearance=None,
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
            last_active_at=NOW - timedelta(minutes=60),
        ),
        proactive_enabled=True,
        # Web-only delivery: the fan-out then needs no channel binding, so
        # these tests turn on the gate's verdict rather than on plumbing.
        accepts_web_proactive=True,
        world_awareness_enabled=True,
    )
    await harness.character_repository.save(updated)
    return updated


def _dispatcher(
    harness,
    *,
    decider: ProactiveDeciderPort,
    gate: _ScriptedGate | None,
    seeds: _StubSeedDispenser | None = None,
) -> tuple[ProactiveDispatcher, InMemoryProactiveAttemptRepository]:
    attempts = InMemoryProactiveAttemptRepository()
    dispatcher = ProactiveDispatcher(
        character_repository=harness.character_repository,
        conversation_repository=harness.conversation_repository,
        account_repository=harness.account_repository,
        binding_repository=harness.binding_repository,
        attempt_repository=attempts,
        gate=HeuristicProactiveGate(
            local_tz=timezone.utc, quiet_hour_start=0, quiet_hour_end=0,
        ),
        decider=decider,
        adapters={
            Platform.TELEGRAM: harness.telegram_adapter,
            Platform.LINE: harness.line_adapter,
        },
        event_seed_dispenser=seeds,
        reply_quality_gate=gate,
        reply_quality_gate_enabled=gate is not None,
        output_quality_orchestrator=(
            OutputQualityOrchestrator(gate=gate) if gate is not None else None
        ),
    )
    return dispatcher, attempts


async def _evaluate(dispatcher, character: Character, *, at: datetime = NOW):
    return await dispatcher.evaluate(
        character_id=character.id, trigger=ProactiveTrigger.TICK, now=at,
    )


def _leaking_gate() -> _ScriptedGate:
    return _ScriptedGate(
        NoveltyVerdict(
            passes=False,
            structural_leak=True,
            feedback="正文混進了 JSON 欄位名，重寫成純粹的角色訊息",
        ),
    )


# --- the withheld tick ----------------------------------------------


@pytest.mark.asyncio
async def test_a_surviving_hard_defect_sends_nothing_and_says_so() -> None:
    harness = build_messaging_harness()
    character = await _web_character(harness)
    decider = _ScriptedDecider(ProactiveDecision(True, "打招呼", LEAKY))
    dispatcher, _ = _dispatcher(harness, decider=decider, gate=_leaking_gate())

    attempt = await _evaluate(dispatcher, character)

    # Its own outcome, not the decider's: the character never chose this.
    assert attempt.outcome == ProactiveOutcome.QUALITY_WITHHELD
    # The defective prose is not on the row either — the gate refused it.
    assert attempt.message is None
    assert harness.telegram_adapter.sent == []
    quality = attempt.metadata["reply_quality_gate"]
    assert quality["outcome"] == OUTCOME_HARD_SKIPPED
    assert quality["structural_leak"] is True
    assert quality["hard_fail"] is True
    # Regenerated once, re-reviewed once — the band's background row.
    assert len(decider.calls) == 2


@pytest.mark.asyncio
async def test_a_withheld_tick_hands_the_event_seed_back() -> None:
    """Nothing referenced the seed, so it must flow on to the next surface.

    Burning it here would starve feed and drama of an inbox row to pay for
    a message no player ever saw.
    """
    harness = build_messaging_harness()
    character = await _web_character(harness)
    seeds = _StubSeedDispenser()
    dispatcher, _ = _dispatcher(
        harness,
        decider=_ScriptedDecider(ProactiveDecision(True, "打招呼", LEAKY)),
        gate=_leaking_gate(),
        seeds=seeds,
    )

    await _evaluate(dispatcher, character)

    assert seeds.released == [("inbox-1", "proactive_message")]


@pytest.mark.asyncio
async def test_a_withheld_tick_does_not_spend_the_daily_quota() -> None:
    harness = build_messaging_harness()
    character = await _web_character(harness)
    dispatcher, attempts = _dispatcher(
        harness,
        decider=_ScriptedDecider(ProactiveDecision(True, "打招呼", LEAKY)),
        gate=_leaking_gate(),
    )

    await _evaluate(dispatcher, character)

    assert await attempts.count_sent_today(character.id, now=NOW) == 0


# --- the cooldown anchor --------------------------------------------


@pytest.mark.asyncio
async def test_a_withheld_tick_does_not_silence_the_cooldown_window() -> None:
    """The confirmed defect: one bad draft muted the character for an hour.

    The withhold is supposed to mean "skip this tick and retry naturally".
    Anchoring the cooldown on it turned it into "go quiet for the whole
    cooldown window", and because the tick logged like an ordinary skip
    nothing in the audit trail said so.
    """
    harness = build_messaging_harness()
    character = await _web_character(harness)
    gate = _ScriptedGate(
        NoveltyVerdict(passes=False, structural_leak=True, feedback="重寫"),
        NoveltyVerdict(passes=False, structural_leak=True, feedback="重寫"),
        NoveltyVerdict(passes=True),
    )
    decider = _ScriptedDecider(
        ProactiveDecision(True, "打招呼", LEAKY),
        ProactiveDecision(True, "打招呼", LEAKY),
        ProactiveDecision(True, "打招呼", CLEAN),
    )
    dispatcher, _ = _dispatcher(harness, decider=decider, gate=gate)

    first = await _evaluate(dispatcher, character)
    second = await _evaluate(dispatcher, character, at=NOW + timedelta(minutes=5))

    assert first.outcome == ProactiveOutcome.QUALITY_WITHHELD
    # Five minutes into a 30-minute cooldown, and the retry still happens.
    assert second.outcome == ProactiveOutcome.SENT
    assert second.message == CLEAN


@pytest.mark.asyncio
async def test_the_characters_own_silence_still_anchors_the_cooldown() -> None:
    """Regression lock on the half that must not move.

    ``DECIDER_SKIPPED`` is the character deciding not to speak. Re-asking
    it five minutes later would both burn budget and overrule it, so the
    fix above must not widen into "no skip anchors the cooldown".
    """
    harness = build_messaging_harness()
    character = await _web_character(harness)
    decider = _ScriptedDecider(
        ProactiveDecision(False, "現在不想打擾", None),
        ProactiveDecision(True, "打招呼", CLEAN),
    )
    dispatcher, _ = _dispatcher(harness, decider=decider, gate=None)

    first = await _evaluate(dispatcher, character)
    second = await _evaluate(dispatcher, character, at=NOW + timedelta(minutes=5))

    assert first.outcome == ProactiveOutcome.DECIDER_SKIPPED
    assert second.outcome == ProactiveOutcome.GATE_BLOCKED
    assert "cooldown" in second.reason
    # The second tick never reached the decider at all.
    assert len(decider.calls) == 1


@pytest.mark.asyncio
async def test_a_clean_send_still_anchors_the_cooldown() -> None:
    harness = build_messaging_harness()
    character = await _web_character(harness)
    decider = _ScriptedDecider(ProactiveDecision(True, "打招呼", CLEAN))
    dispatcher, _ = _dispatcher(
        harness, decider=decider, gate=_ScriptedGate(NoveltyVerdict(passes=True)),
    )

    first = await _evaluate(dispatcher, character)
    second = await _evaluate(dispatcher, character, at=NOW + timedelta(minutes=5))

    assert first.outcome == ProactiveOutcome.SENT
    assert second.outcome == ProactiveOutcome.GATE_BLOCKED
