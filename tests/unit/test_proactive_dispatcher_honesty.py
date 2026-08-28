"""HV2 — the outbound honesty gate on the proactive surface.

The proactive decider is structurally worse-placed than the promise loop
for this class of failure: it writes the message in the *same* JSON that
orders the tool, so the prose is composed before the tool has run and can
never know whether it worked. 「拍了張照片給你」 is therefore a claim about
the future at the moment it is written and a claim about the past by the
time it lands — and on the (common) tick where ComfyUI is down, the old
behaviour shipped it anyway with nothing attached.

These tests drive the dispatcher end-to-end with a scripted judge, so
what they pin is the *dispatcher's* behaviour on each verdict — not the
judge's semantics, which are HV1's tests and belong there.
"""

from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.application.services.outcome_claim_audit import (
    OUTCOME_CLAIM_PARK_AUTONOMOUS_SILENCE,
    OUTCOME_CLAIM_PARK_JUDGE_UNAVAILABLE,
    OUTCOME_CLAIM_PARK_MODEL_REOFFENDED,
)
from kokoro_link.application.services.outcome_claim_guard import (
    OutcomeClaimGuard,
)
from kokoro_link.application.services.proactive_dispatcher import (
    ProactiveDispatcher,
)
from kokoro_link.contracts.outcome_claim import (
    OutcomeClaimEvidence,
    OutcomeClaimJudgePort,
    OutcomeClaimVerdict,
)
from kokoro_link.contracts.prompt import PromptToolDescriptor
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
from kokoro_link.domain.value_objects.tool_call import (
    ToolAttachment,
    ToolCall,
    ToolResult,
)
from kokoro_link.infrastructure.proactive.heuristic_gate import (
    HeuristicProactiveGate,
)
from kokoro_link.infrastructure.repositories.in_memory_proactive_attempts import (
    InMemoryProactiveAttemptRepository,
)
from tests.unit._messaging_harness import build_messaging_harness, create_character

PHOTO_TOOL = "generate_image"
CLAIMED = "拍好了，照片傳給你囉"
HONEST = "等等拍給你看"


# --- doubles ---------------------------------------------------------


class _ScriptedJudge(OutcomeClaimJudgePort):
    """Answers from a queue; the last answer repeats once exhausted."""

    def __init__(self, *verdicts: OutcomeClaimVerdict) -> None:
        self._verdicts = list(verdicts)
        self.seen: list[tuple[str, OutcomeClaimEvidence]] = []

    async def judge(
        self,
        *,
        message_text: str,
        evidence: OutcomeClaimEvidence,
        character: Character | None = None,
        operator_primary_language: str = "",
    ) -> OutcomeClaimVerdict:
        self.seen.append((message_text, evidence))
        if len(self._verdicts) > 1:
            return self._verdicts.pop(0)
        return self._verdicts[0]


class _ScriptedDecider(ProactiveDeciderPort):
    """One decision per call; the last one repeats."""

    def __init__(self, *decisions: ProactiveDecision) -> None:
        self._decisions = list(decisions)
        self.calls: list[ProactiveContext] = []

    async def decide(self, context: ProactiveContext) -> ProactiveDecision:
        self.calls.append(context)
        if len(self._decisions) > 1:
            return self._decisions.pop(0)
        return self._decisions[0]


class _StubToolRegistry:
    def __init__(self) -> None:
        self._tool = PromptToolDescriptor(
            name=PHOTO_TOOL,
            description="拍一張此刻的照片。",
            parameters_schema={"type": "object", "properties": {}},
        )

    def all(self):  # pragma: no cover - unused by the dispatcher
        return []

    def get(self, name: str):  # pragma: no cover - unused
        return None

    def list_for_character(self, character):
        return [self._tool]


class _StubOrchestrator:
    """Runs nothing; returns the scripted result and counts the calls."""

    def __init__(self, result: ToolResult) -> None:
        self._result = result
        self.executed: list[ToolCall] = []

    async def execute(self, *, character, call, conversation_id=None, **_):
        self.executed.append(call)
        return object(), self._result


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
            last_active_at=datetime.now(timezone.utc) - timedelta(minutes=60),
        ),
        proactive_enabled=True,
        # Web delivery only: the fan-out then needs no channel binding, so
        # these tests fail on the gate's verdict rather than on plumbing.
        accepts_web_proactive=True,
    )
    await harness.character_repository.save(updated)
    return updated


def _dispatcher(
    harness,
    *,
    decider: ProactiveDeciderPort,
    guard: OutcomeClaimGuard | None,
    orchestrator: _StubOrchestrator | None = None,
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
        tool_registry=_StubToolRegistry() if orchestrator else None,
        tool_orchestrator=orchestrator,
        outcome_claim_guard=guard,
    )
    return dispatcher, attempts


async def _evaluate(dispatcher, character: Character):
    return await dispatcher.evaluate(
        character_id=character.id, trigger=ProactiveTrigger.TICK,
    )


def _guard(*verdicts: OutcomeClaimVerdict) -> tuple[OutcomeClaimGuard, _ScriptedJudge]:
    judge = _ScriptedJudge(*verdicts)
    return OutcomeClaimGuard(judge=judge), judge


# --- no guard = no change -------------------------------------------


@pytest.mark.asyncio
async def test_without_a_guard_the_push_behaves_exactly_as_before() -> None:
    harness = build_messaging_harness()
    character = await _web_character(harness)
    decider = _ScriptedDecider(ProactiveDecision(True, "ok", CLAIMED))
    dispatcher, _ = _dispatcher(harness, decider=decider, guard=None)

    attempt = await _evaluate(dispatcher, character)

    assert attempt.outcome == ProactiveOutcome.SENT
    assert len(decider.calls) == 1


# --- the consistent path --------------------------------------------


@pytest.mark.asyncio
async def test_a_consistent_message_ships_and_costs_one_verdict() -> None:
    harness = build_messaging_harness()
    character = await _web_character(harness)
    decider = _ScriptedDecider(ProactiveDecision(True, "ok", HONEST))
    guard, judge = _guard(OutcomeClaimVerdict.ok())
    dispatcher, _ = _dispatcher(harness, decider=decider, guard=guard)

    attempt = await _evaluate(dispatcher, character)

    assert attempt.outcome == ProactiveOutcome.SENT
    assert len(judge.seen) == 1
    assert guard.counters.consistent == 1
    assert guard.counters.parked == 0


# --- the zero-call exit ---------------------------------------------


@pytest.mark.asyncio
async def test_a_zero_call_overclaim_is_re_decided_once_and_ships_if_honest() -> None:
    harness = build_messaging_harness()
    character = await _web_character(harness)
    decider = _ScriptedDecider(
        ProactiveDecision(True, "ok", CLAIMED),
        ProactiveDecision(True, "ok", HONEST),
    )
    guard, _ = _guard(
        OutcomeClaimVerdict.blocked(("照片傳給你囉",)),
        OutcomeClaimVerdict.ok(),
    )
    dispatcher, _ = _dispatcher(harness, decider=decider, guard=guard)

    attempt = await _evaluate(dispatcher, character)

    assert attempt.outcome == ProactiveOutcome.SENT
    assert attempt.message == HONEST
    assert len(decider.calls) == 2
    assert guard.counters.blocked_zero_call == 1
    assert guard.counters.corrected == 1


@pytest.mark.asyncio
async def test_the_re_decide_carries_the_correction_and_nothing_else() -> None:
    """The correction rides its own field, not the dialogue summary.

    Folded into ``recent_dialogue_summary`` (the idiom the quality gate
    reuses) it would arrive where the prompt says the two of them were
    talking, and the model reads a system reprimand as something the
    player said.
    """
    harness = build_messaging_harness()
    character = await _web_character(harness)
    decider = _ScriptedDecider(
        ProactiveDecision(True, "ok", CLAIMED),
        ProactiveDecision(True, "ok", HONEST),
    )
    guard, _ = _guard(
        OutcomeClaimVerdict.blocked(("照片傳給你囉",)),
        OutcomeClaimVerdict.ok(),
    )
    dispatcher, _ = _dispatcher(harness, decider=decider, guard=guard)

    await _evaluate(dispatcher, character)

    first, retry = decider.calls
    assert first.honesty_correction == ""
    assert "照片傳給你囉" in retry.honesty_correction
    assert retry.recent_dialogue_summary == first.recent_dialogue_summary
    # F3: the decider is a single JSON object (should_send/message/
    # tool_calls together), so the correction must not tell it to answer
    # with "tool JSON only, no message text" — an empty ``message`` makes
    # LLMProactiveDecider downgrade to should_send=False before the
    # tool_calls it asked for are ever read, silently discarding the
    # "actually call the tool" road this correction is supposed to offer.
    assert "不要寫任何訊息內容" not in retry.honesty_correction
    assert "tool_calls" in retry.honesty_correction


@pytest.mark.asyncio
async def test_a_re_offending_correction_sends_nothing() -> None:
    harness = build_messaging_harness()
    character = await _web_character(harness)
    decider = _ScriptedDecider(ProactiveDecision(True, "ok", CLAIMED))
    guard, _ = _guard(OutcomeClaimVerdict.blocked(("照片傳給你囉",)))
    dispatcher, _ = _dispatcher(harness, decider=decider, guard=guard)

    attempt = await _evaluate(dispatcher, character)

    assert attempt.outcome == ProactiveOutcome.DECIDER_SKIPPED
    assert harness.telegram_adapter.sent == []
    assert guard.counters.parked == 1
    # B-4: reoffended after a correction attempt — model_reoffended, not
    # our judge's fault.
    audit = attempt.metadata["outcome_claim"]
    assert audit["parked_kind"] == OUTCOME_CLAIM_PARK_MODEL_REOFFENDED


@pytest.mark.asyncio
async def test_a_correction_that_chooses_silence_is_not_an_error() -> None:
    harness = build_messaging_harness()
    character = await _web_character(harness)
    decider = _ScriptedDecider(
        ProactiveDecision(True, "ok", CLAIMED),
        ProactiveDecision(False, "想想還是算了", None),
    )
    guard, _ = _guard(OutcomeClaimVerdict.blocked(("照片傳給你囉",)))
    dispatcher, _ = _dispatcher(harness, decider=decider, guard=guard)

    attempt = await _evaluate(dispatcher, character)

    assert attempt.outcome == ProactiveOutcome.DECIDER_SKIPPED
    assert guard.counters.parked == 1
    # B-4: shown its own overclaim, the character chose silence — an
    # honest road, not the model reoffending, so it gets its own kind.
    audit = attempt.metadata["outcome_claim"]
    assert audit["parked_kind"] == OUTCOME_CLAIM_PARK_AUTONOMOUS_SILENCE


@pytest.mark.asyncio
async def test_a_crashing_correction_re_decide_parks_as_model_reoffended() -> None:
    """The re-decide call itself blows up rather than answering.

    Nothing came back to inspect, but the *first* verdict already
    established the model's fault — the crash is one more failed attempt
    to correct that same offence, not a fresh, blameless one, so it still
    counts as model_reoffended rather than judge_unavailable (our judge
    answered fine; it is the decider that broke)."""

    class _CrashesOnRetry(ProactiveDeciderPort):
        def __init__(self) -> None:
            self.calls = 0

        async def decide(self, context: ProactiveContext) -> ProactiveDecision:
            self.calls += 1
            if self.calls == 1:
                return ProactiveDecision(True, "ok", CLAIMED)
            raise RuntimeError("decider upstream is down")

    harness = build_messaging_harness()
    character = await _web_character(harness)
    decider = _CrashesOnRetry()
    guard, _ = _guard(OutcomeClaimVerdict.blocked(("照片傳給你囉",)))
    dispatcher, _ = _dispatcher(harness, decider=decider, guard=guard)

    attempt = await _evaluate(dispatcher, character)

    assert attempt.outcome == ProactiveOutcome.DECIDER_SKIPPED
    assert decider.calls == 2
    assert guard.counters.parked == 1
    audit = attempt.metadata["outcome_claim"]
    assert audit["parked_kind"] == OUTCOME_CLAIM_PARK_MODEL_REOFFENDED


# --- the after-tools exit -------------------------------------------


@pytest.mark.asyncio
async def test_a_failed_render_no_longer_ships_the_message_that_claimed_it() -> None:
    """The production shape: text written before the tool, tool then dies.

    The old comment on ``_execute_decision_tools`` said failures are
    dropped so "the text message still goes out on its own" — which is
    right when the text does not depend on the picture, and a lie when it
    does. The judge is the only thing that can tell those apart.
    """
    harness = build_messaging_harness()
    character = await _web_character(harness)
    decider = _ScriptedDecider(
        ProactiveDecision(
            True, "ok", CLAIMED, tool_calls=(ToolCall(name=PHOTO_TOOL),),
        ),
    )
    guard, judge = _guard(OutcomeClaimVerdict.blocked(("照片傳給你囉",)))
    orchestrator = _StubOrchestrator(ToolResult.failure("comfyui unreachable"))
    dispatcher, _ = _dispatcher(
        harness, decider=decider, guard=guard, orchestrator=orchestrator,
    )

    attempt = await _evaluate(dispatcher, character)

    assert attempt.outcome == ProactiveOutcome.DECIDER_SKIPPED
    # No second decide: the decider never sees a tool result, so a rewrite
    # would be as blind as the first pass and free to order a second render.
    assert len(decider.calls) == 1
    assert len(orchestrator.executed) == 1
    assert guard.counters.blocked_after_tools == 1
    # The failure reached the judge as a fact rather than only a log line.
    _, evidence = judge.seen[0]
    assert evidence.outcomes[0].ok is False
    assert evidence.delivered_attachments == 0
    assert evidence.offered_tools == (PHOTO_TOOL,)
    # B-4: the decider composes blind to the tool result, so there is no
    # second pass to spend after tools ran — model_reoffended, same class
    # as every other "correction failed to salvage" park.
    audit = attempt.metadata["outcome_claim"]
    assert audit["parked_kind"] == OUTCOME_CLAIM_PARK_MODEL_REOFFENDED


@pytest.mark.asyncio
async def test_a_delivered_render_backs_the_claim_and_ships() -> None:
    harness = build_messaging_harness()
    character = await _web_character(harness)
    decider = _ScriptedDecider(
        ProactiveDecision(
            True, "ok", CLAIMED, tool_calls=(ToolCall(name=PHOTO_TOOL),),
        ),
    )
    guard, judge = _guard(OutcomeClaimVerdict.ok())
    orchestrator = _StubOrchestrator(
        ToolResult.success(
            "畫好了",
            attachments=(
                ToolAttachment(
                    kind="image",
                    url="https://cdn.example.com/a.png",
                    mime_type="image/png",
                ),
            ),
        ),
    )
    dispatcher, _ = _dispatcher(
        harness, decider=decider, guard=guard, orchestrator=orchestrator,
    )

    attempt = await _evaluate(dispatcher, character)

    assert attempt.outcome == ProactiveOutcome.SENT
    _, evidence = judge.seen[0]
    assert evidence.delivered_attachments == 1


# --- fail direction --------------------------------------------------


@pytest.mark.asyncio
async def test_an_unavailable_verdict_is_not_an_approval() -> None:
    """Fail-closed: a judge that cannot answer withholds the push.

    The alternative — treating a dead judge as consent — makes the gate
    silently absent exactly when its upstream is having a bad day, which
    is the state it is least safe to be absent in.
    """
    harness = build_messaging_harness()
    character = await _web_character(harness)
    decider = _ScriptedDecider(ProactiveDecision(True, "ok", CLAIMED))
    guard, _ = _guard(OutcomeClaimVerdict.failed())
    dispatcher, _ = _dispatcher(harness, decider=decider, guard=guard)

    attempt = await _evaluate(dispatcher, character)

    assert attempt.outcome == ProactiveOutcome.DECIDER_SKIPPED
    assert len(decider.calls) == 1
    assert guard.counters.judge_failed == 1
    assert guard.counters.parked == 1
    # B-4: nothing is known about the model here, only that our judge
    # never answered — judge_unavailable, not model_reoffended.
    audit = attempt.metadata["outcome_claim"]
    assert audit["parked_kind"] == OUTCOME_CLAIM_PARK_JUDGE_UNAVAILABLE


# --- the HV3 audit trail --------------------------------------------


@pytest.mark.asyncio
async def test_a_withheld_push_leaves_an_audit_row_saying_why() -> None:
    """The park is the case that otherwise leaves no trace at all.

    A blocked round sends nothing, so there is no message, no turn and no
    delivery to read afterwards — and a dishonesty rate computed from the
    audit table would quietly exclude the highest-volume outbound surface
    unless this tick opens HV3's scope.
    """
    harness = build_messaging_harness()
    character = await _web_character(harness)
    decider = _ScriptedDecider(ProactiveDecision(True, "ok", CLAIMED))
    guard, _ = _guard(OutcomeClaimVerdict.blocked(("照片傳給你囉",)))
    dispatcher, _ = _dispatcher(harness, decider=decider, guard=guard)

    attempt = await _evaluate(dispatcher, character)

    audit = attempt.metadata["outcome_claim"]
    assert audit["final_verdict"] == "inconsistent"
    assert audit["blocked_count"] == 1
    assert audit["blocked_after_tools"] is False
    assert audit["parked"] is True
    assert audit["parked_reason"] == "correction overclaimed again"
    # The redline: counts and enum strings only, never the quoted claim.
    assert "照片傳給你囉" not in repr(audit)


@pytest.mark.asyncio
async def test_an_ungated_deployment_writes_no_audit_key_at_all() -> None:
    harness = build_messaging_harness()
    character = await _web_character(harness)
    decider = _ScriptedDecider(ProactiveDecision(True, "ok", CLAIMED))
    dispatcher, _ = _dispatcher(harness, decider=decider, guard=None)

    attempt = await _evaluate(dispatcher, character)

    assert "outcome_claim" not in attempt.metadata


@pytest.mark.asyncio
async def test_a_corrected_send_records_both_verdicts_in_order() -> None:
    harness = build_messaging_harness()
    character = await _web_character(harness)
    decider = _ScriptedDecider(
        ProactiveDecision(True, "ok", CLAIMED),
        ProactiveDecision(True, "ok", HONEST),
    )
    guard, _ = _guard(
        OutcomeClaimVerdict.blocked(("照片傳給你囉",)),
        OutcomeClaimVerdict.ok(),
    )
    dispatcher, _ = _dispatcher(harness, decider=decider, guard=guard)

    attempt = await _evaluate(dispatcher, character)

    assert attempt.outcome == ProactiveOutcome.SENT
    audit = attempt.metadata["outcome_claim"]
    assert audit["verdicts"] == ["inconsistent", "consistent"]
    assert audit["corrected_count"] == 1
    assert audit["parked"] is False


@pytest.mark.asyncio
async def test_a_judge_that_raises_does_not_kill_the_tick() -> None:
    class _Exploding(OutcomeClaimJudgePort):
        async def judge(self, **_):
            raise RuntimeError("boom")

    harness = build_messaging_harness()
    character = await _web_character(harness)
    decider = _ScriptedDecider(ProactiveDecision(True, "ok", CLAIMED))
    guard = OutcomeClaimGuard(judge=_Exploding())
    dispatcher, _ = _dispatcher(harness, decider=decider, guard=guard)

    attempt = await _evaluate(dispatcher, character)

    # Skipped, not ERRORED: a gate must never turn a model hiccup into an
    # exception the operator has to triage as a pipeline failure.
    assert attempt.outcome == ProactiveOutcome.DECIDER_SKIPPED
