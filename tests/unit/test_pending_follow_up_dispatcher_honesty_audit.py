"""HV3 — the HV1 honesty gate's verdict lands in a turn record.

Mirrors the harness in ``test_pending_follow_up_dispatcher_promise.py``
(``ComposerToolLoop`` wired with a real ``OutcomeClaimGuard`` and a
scripted judge) but asserts what reaches the ``turn_recorder`` rather than
what reaches the player. Two shapes matter most:

* a round the gate blocked-then-corrected still ships, AND its judge
  trail is recorded alongside the send;
* a round the gate parks ships NOTHING — the case every other turn-record
  call site in this dispatcher would otherwise miss entirely, because
  ``deliver_pre_composed`` is never reached.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from kokoro_link.application.services.composer_tool_loop import ComposerToolLoop
from kokoro_link.application.services.outcome_claim_guard import OutcomeClaimGuard
from kokoro_link.application.services.pending_follow_up_dispatcher import (
    PendingFollowUpDispatcher,
)
from kokoro_link.application.services.tool_orchestrator import ToolOrchestrator
from kokoro_link.contracts.observability import TurnRecordingDraft
from kokoro_link.contracts.outcome_claim import OutcomeClaimVerdict
from kokoro_link.contracts.pending_follow_up_composer import (
    PendingFollowUpComposeOutput,
    PendingFollowUpComposerPort,
)
from kokoro_link.contracts.scheduled_promise_composer import (
    ScheduledPromiseComposeInput,
    ScheduledPromiseComposeOutput,
    ScheduledPromiseComposerPort,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.pending_follow_up import (
    PendingFollowUp,
    PendingFollowUpStatus,
)
from kokoro_link.domain.entities.proactive_attempt import ProactiveAttempt
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.proactive_outcome import ProactiveOutcome
from kokoro_link.domain.value_objects.tool_call import ToolCall
from kokoro_link.infrastructure.repositories.in_memory_pending_follow_ups import (
    InMemoryPendingFollowUpRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_tool_invocations import (
    InMemoryToolInvocationRepository,
)
from kokoro_link.infrastructure.tools.fake_tools import FakeImageTool
from kokoro_link.infrastructure.tools.registry import InMemoryToolRegistry


def _now() -> datetime:
    return datetime(2026, 8, 25, 9, 36, tzinfo=timezone.utc)


def _character(cid: str = "char-1") -> Character:
    return replace(
        Character.create(
            name="Aki", summary="", personality=[], interests=[],
            speaking_style="", boundaries=[],
            state=CharacterState(
                emotion="neutral", affection=50, fatigue=20, trust=50, energy=70,
            ),
        ),
        id=cid, allowed_tools=["fake_image"],
    )


def _promise_row(*, character_id: str = "char-1") -> PendingFollowUp:
    return PendingFollowUp.new_promise(
        character_id=character_id,
        conversation_id="conv-1",
        promise_intent="晚點畫一張圖傳給對方",
        scheduled_for=_now() - timedelta(minutes=1),
        source_message_content="等等畫一張給我看",
        now=_now() - timedelta(hours=1),
    )


@dataclass
class _StubCharacterRepo:
    characters: dict[str, Character]

    async def get(self, character_id: str) -> Character | None:
        return self.characters.get(character_id)


class _StubBusyComposer(PendingFollowUpComposerPort):
    async def compose(self, payload):  # pragma: no cover - never called
        return PendingFollowUpComposeOutput(content_text="should not run")


class _StubScheduleService:
    async def ensure_schedule(self, character):
        return object()

    def resolve_current(self, schedule, *, now):
        return None, [], None


@dataclass
class _ScriptedPromiseComposer(ScheduledPromiseComposerPort):
    """Answers a script keyed on which pass this is, matching the
    fixture used by ``test_pending_follow_up_dispatcher_promise``."""

    script: list[str]
    tool_calls: tuple[ToolCall, ...] = (
        ToolCall(name="fake_image", arguments={"scene": "廚房"}),
    )
    calls: list[ScheduledPromiseComposeInput] = field(default_factory=list)

    async def compose(self, payload):
        self.calls.append(payload)
        if payload.tool_results:
            return ScheduledPromiseComposeOutput(content_text=self.script.pop(0))
        if payload.available_tools:
            text = self.script[0]
            if text == "__CALL__":
                self.script.pop(0)
                return ScheduledPromiseComposeOutput(
                    content_text="", tool_calls=self.tool_calls,
                )
            return ScheduledPromiseComposeOutput(content_text=self.script.pop(0))
        return ScheduledPromiseComposeOutput(content_text=self.script.pop(0))


@dataclass
class _ScriptedJudge:
    verdicts: list[OutcomeClaimVerdict]

    async def judge(self, **_kwargs) -> OutcomeClaimVerdict:
        return self.verdicts.pop(0)


class _StubProactiveDispatcher:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def deliver_pre_composed(
        self, *, character_id, text, trigger, reason="", attachments=(), now=None,
    ):
        self.calls.append({"character_id": character_id, "text": text})
        return ProactiveAttempt.record(
            character_id=character_id, trigger=trigger,
            outcome=ProactiveOutcome.SENT, reason=reason or "stub",
            message=text, now=now or _now(),
        )


class _RecordingTurnRecorder:
    def __init__(self) -> None:
        self.drafts: list[TurnRecordingDraft] = []

    async def record(self, draft: TurnRecordingDraft) -> str:
        self.drafts.append(draft)
        return draft.id or "rec-1"


def _tool_loop(judge_verdicts: list[OutcomeClaimVerdict]) -> ComposerToolLoop:
    registry = InMemoryToolRegistry([FakeImageTool()])
    guard = OutcomeClaimGuard(judge=_ScriptedJudge(judge_verdicts))
    return ComposerToolLoop(
        tool_registry=registry,
        tool_orchestrator=ToolOrchestrator(
            registry=registry,
            invocation_repository=InMemoryToolInvocationRepository(),
        ),
        public_base_url="https://yura.example",
        outcome_claim_guard=guard,
    )


def _dispatcher(
    *,
    repo, character, composer, proactive, tool_loop, turn_recorder,
) -> PendingFollowUpDispatcher:
    return PendingFollowUpDispatcher(
        repository=repo,
        composer=_StubBusyComposer(),
        proactive_dispatcher=proactive,
        character_repository=_StubCharacterRepo({character.id: character}),
        schedule_service=_StubScheduleService(),
        scheduled_promise_composer=composer,
        tool_loop=tool_loop,
        turn_recorder=turn_recorder,
    )


@pytest.mark.asyncio
async def test_a_blocked_then_corrected_round_ships_and_is_audited() -> None:
    repo = InMemoryPendingFollowUpRepository()
    row = _promise_row()
    await repo.add(row)
    char = _character()
    proactive = _StubProactiveDispatcher()
    composer = _ScriptedPromiseComposer(
        script=["畫好囉！圖片附上～", "圖畫好了，你看看喜不喜歡"],
    )
    turn_recorder = _RecordingTurnRecorder()
    loop = _tool_loop([
        OutcomeClaimVerdict.blocked(("附上",)),
        OutcomeClaimVerdict.ok(),
    ])
    dispatcher = _dispatcher(
        repo=repo, character=char, composer=composer, proactive=proactive,
        tool_loop=loop, turn_recorder=turn_recorder,
    )

    assert await dispatcher.tick(now=_now()) == 1
    assert proactive.calls[0]["text"] == "圖畫好了，你看看喜不喜歡"

    assert len(turn_recorder.drafts) == 1
    draft = turn_recorder.drafts[0]
    assert draft.kind == "promise_fulfilment"
    assert draft.character_id == char.id
    refs = draft.post_turn_refs
    assert refs["pending_follow_up_id"] == row.id
    audit = refs["outcome_claim_judge"]
    # Two judge calls: the zero-call offence, then the retry's prose —
    # which the judge cleared — is reviewed again before it ships.
    assert audit["verdicts"] == ["inconsistent", "consistent"]
    assert audit["final_verdict"] == "consistent"
    assert audit["blocked_count"] == 1
    assert audit["corrected_count"] == 1
    assert audit["parked"] is False
    # The red line: never the claim text, never the message body.
    assert "附上" not in str(refs)
    assert "圖畫好了" not in str(refs)


@pytest.mark.asyncio
async def test_a_parked_round_sends_nothing_but_is_still_audited() -> None:
    """The case no other write path in this dispatcher would catch: the
    round never reaches ``deliver_pre_composed`` at all."""
    repo = InMemoryPendingFollowUpRepository()
    row = _promise_row()
    await repo.add(row)
    char = _character()
    proactive = _StubProactiveDispatcher()
    composer = _ScriptedPromiseComposer(
        script=["圖片已經傳過去囉", "真的傳過去了"],
    )
    turn_recorder = _RecordingTurnRecorder()
    loop = _tool_loop([
        OutcomeClaimVerdict.blocked(("傳過去囉",)),
        OutcomeClaimVerdict.blocked(("傳過去了",)),
    ])
    dispatcher = _dispatcher(
        repo=repo, character=char, composer=composer, proactive=proactive,
        tool_loop=loop, turn_recorder=turn_recorder,
    )

    assert await dispatcher.tick(now=_now()) == 0
    assert proactive.calls == []
    stored = await repo.get(row.id)
    assert stored is not None
    assert stored.status == PendingFollowUpStatus.QUEUED

    assert len(turn_recorder.drafts) == 1
    audit = turn_recorder.drafts[0].post_turn_refs["outcome_claim_judge"]
    assert audit["blocked_count"] == 1
    assert audit["corrected_count"] == 0
    assert audit["parked"] is True
    assert audit["parked_reason"] == "correction pass claimed an outcome again"


@pytest.mark.asyncio
async def test_a_consistent_round_is_still_audited() -> None:
    """Even the overwhelmingly common "nothing was wrong" case gets a
    row — a dishonesty rate needs the denominator, not only the
    numerator."""
    repo = InMemoryPendingFollowUpRepository()
    row = _promise_row()
    await repo.add(row)
    char = _character()
    proactive = _StubProactiveDispatcher()
    composer = _ScriptedPromiseComposer(script=["晚點畫給你！"])
    turn_recorder = _RecordingTurnRecorder()
    loop = _tool_loop([OutcomeClaimVerdict.ok()])
    dispatcher = _dispatcher(
        repo=repo, character=char, composer=composer, proactive=proactive,
        tool_loop=loop, turn_recorder=turn_recorder,
    )

    assert await dispatcher.tick(now=_now()) == 1
    assert len(turn_recorder.drafts) == 1
    audit = turn_recorder.drafts[0].post_turn_refs["outcome_claim_judge"]
    assert audit["verdicts"] == ["consistent"]
    assert audit["blocked_count"] == 0
    assert audit["parked"] is False


@pytest.mark.asyncio
async def test_no_turn_recorder_wired_is_a_silent_no_op() -> None:
    """Byte-compatible with every dispatcher constructed before HV3."""
    repo = InMemoryPendingFollowUpRepository()
    await repo.add(_promise_row())
    char = _character()
    proactive = _StubProactiveDispatcher()
    composer = _ScriptedPromiseComposer(script=["晚點畫給你！"])
    loop = _tool_loop([OutcomeClaimVerdict.ok()])
    dispatcher = _dispatcher(
        repo=repo, character=char, composer=composer, proactive=proactive,
        tool_loop=loop, turn_recorder=None,
    )

    assert await dispatcher.tick(now=_now()) == 1  # unaffected


@pytest.mark.asyncio
async def test_no_tool_loop_records_nothing_for_the_gate() -> None:
    """A caller with no loop wired never reaches the guard at all — the
    audit write must not fabricate a verdict out of thin air."""
    repo = InMemoryPendingFollowUpRepository()
    await repo.add(_promise_row())
    char = _character()
    proactive = _StubProactiveDispatcher()
    composer = _ScriptedPromiseComposer(script=["晚點畫給你！"])
    turn_recorder = _RecordingTurnRecorder()
    dispatcher = _dispatcher(
        repo=repo, character=char, composer=composer, proactive=proactive,
        tool_loop=None, turn_recorder=turn_recorder,
    )

    assert await dispatcher.tick(now=_now()) == 1
    assert turn_recorder.drafts == []
