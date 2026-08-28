"""F1 — what an honesty park costs the row it parked.

Before this, every park took the same exit an ordinary composer fail-soft
takes: ``marked_failed`` → back to ``queued``, ``scheduled_for`` untouched,
no counter. Two things followed, and both are pinned here.

* A model that claims outcomes it did not produce got asked again on the
  very next tick, forever. Nothing about the row changed between attempts,
  so nothing about the answer would either — an unbounded compose spend on
  a promise that was never going to be kept.
* Because ``list_due`` is ``ORDER BY scheduled_for`` with a limit, a row
  whose release instant stays permanently in the past also holds the head
  of the queue. Enough of them and newer, releasable promises never get
  looked at at all.

The split that fixes it is the one thing these tests care most about: a
park **our judge** caused must be delayed but never charged. Confiscating
promises because our own upstream is down would be a worse bug than the
one being fixed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from kokoro_link.application.services.composer_tool_loop import ComposerToolLoop
from kokoro_link.application.services.outcome_claim_guard import OutcomeClaimGuard
from kokoro_link.application.services.pending_follow_up_dispatcher import (
    HONESTY_JUDGE_OUTAGE_RETRY_SECONDS,
    HONESTY_PARK_ATTEMPT_LIMIT,
    HONESTY_REOFFENCE_RETRY_SECONDS,
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
    HONESTY_REPAIR_DEFER_REASON,
    PendingFollowUp,
    PendingFollowUpMessage,
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


_DISPATCHER_LOGGER = (
    "kokoro_link.application.services.pending_follow_up_dispatcher"
)


def _now() -> datetime:
    return datetime(2026, 8, 26, 9, 0, tzinfo=timezone.utc)


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


@dataclass
class _LoopingBusyComposer(PendingFollowUpComposerPort):
    """The busy-defer twin of :class:`_LoopingPromiseComposer`.

    Present because the two release methods are separate code paths with
    the same tail — a fix applied to one and forgotten in the other is
    exactly the kind of drift only a test on both sides catches."""

    text: str = "已經幫你查到了喔"

    async def compose(self, payload):
        return PendingFollowUpComposeOutput(content_text=self.text)


class _StubScheduleService:
    async def ensure_schedule(self, character):
        return object()

    def resolve_current(self, schedule, *, now):
        return None, [], None


@dataclass
class _LoopingPromiseComposer(ScheduledPromiseComposerPort):
    """Always answers with the same unsupported claim, on every pass.

    Not a script that runs out: the defect is about a model that keeps
    doing the same thing tick after tick, so the fixture has to be able to
    do the same thing tick after tick too."""

    text: str = "圖片已經傳過去囉"
    calls: list[ScheduledPromiseComposeInput] = field(default_factory=list)

    async def compose(self, payload):
        self.calls.append(payload)
        return ScheduledPromiseComposeOutput(content_text=self.text)


@dataclass
class _ToolCallingComposer(ScheduledPromiseComposerPort):
    """Pass 1 asks for the image tool — the PF3 hand-off shape."""

    calls: list[ScheduledPromiseComposeInput] = field(default_factory=list)

    async def compose(self, payload):
        self.calls.append(payload)
        if payload.available_tools:
            return ScheduledPromiseComposeOutput(
                content_text="",
                tool_calls=(ToolCall(name="fake_image", arguments={}),),
            )
        return ScheduledPromiseComposeOutput(content_text="畫好了")


@dataclass
class _SilentComposer(ScheduledPromiseComposerPort):
    """The pre-existing composer fail-soft: empty text, no gate involved."""

    calls: int = 0

    async def compose(self, payload):
        self.calls += 1
        return ScheduledPromiseComposeOutput(content_text="")


@dataclass
class _AlwaysJudge:
    """One verdict, returned for every review this round and every round."""

    verdict_factory: Any

    async def judge(self, **_kwargs) -> OutcomeClaimVerdict:
        return self.verdict_factory()


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


class _StubCapabilityEnqueuer:
    """Always takes the capability half — the PF3 queue being available."""

    def __init__(self) -> None:
        self.deferred: list[str] = []

    async def defer_capability(self, row, *, capability, now) -> bool:
        self.deferred.append(capability)
        return True


def _tool_loop(guard: OutcomeClaimGuard | None) -> ComposerToolLoop:
    registry = InMemoryToolRegistry([FakeImageTool()])
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
    repo,
    character: Character,
    composer,
    proactive,
    tool_loop,
    guard: OutcomeClaimGuard | None = None,
    turn_recorder=None,
    busy_composer: PendingFollowUpComposerPort | None = None,
) -> PendingFollowUpDispatcher:
    return PendingFollowUpDispatcher(
        repository=repo,
        composer=busy_composer or _StubBusyComposer(),
        proactive_dispatcher=proactive,
        character_repository=_StubCharacterRepo({character.id: character}),
        schedule_service=_StubScheduleService(),
        scheduled_promise_composer=composer,
        tool_loop=tool_loop,
        outcome_claim_guard=guard,
        turn_recorder=turn_recorder,
    )


def _reoffending_setup(*, turn_recorder=None):
    """A model that re-claims an unsupported outcome on every pass."""
    guard = OutcomeClaimGuard(
        judge=_AlwaysJudge(lambda: OutcomeClaimVerdict.blocked(("傳過去",))),
    )
    repo = InMemoryPendingFollowUpRepository()
    char = _character()
    proactive = _StubProactiveDispatcher()
    dispatcher = _dispatcher(
        repo=repo, character=char, composer=_LoopingPromiseComposer(),
        proactive=proactive, tool_loop=_tool_loop(guard), guard=guard,
        turn_recorder=turn_recorder,
    )
    return repo, dispatcher, proactive, guard


@pytest.mark.asyncio
async def test_a_model_reoffence_park_delays_the_retry_and_charges_one_attempt(
) -> None:
    repo, dispatcher, proactive, _guard = _reoffending_setup()
    row = _promise_row()
    await repo.add(row)

    assert await dispatcher.tick(now=_now()) == 0
    assert proactive.calls == []

    stored = await repo.get(row.id)
    assert stored is not None
    assert stored.status == PendingFollowUpStatus.QUEUED
    assert stored.honesty_park_attempts == 1
    assert stored.scheduled_for == _now() + timedelta(
        seconds=HONESTY_REOFFENCE_RETRY_SECONDS,
    )
    # ``last_error`` is the only place an operator reading the row sees
    # why it went quiet, so it names both the class and the budget.
    assert stored.last_error is not None
    assert "model_reoffended" in stored.last_error
    assert f"1/{HONESTY_PARK_ATTEMPT_LIMIT}" in stored.last_error


@pytest.mark.asyncio
async def test_the_parked_row_stops_holding_the_head_of_the_due_queue() -> None:
    """S3: ``list_due`` is ordered by ``scheduled_for`` and limited, so a
    permanently-overdue row is not merely retried too often — it is served
    ahead of every newer promise, every tick, forever."""
    repo, dispatcher, _proactive, _guard = _reoffending_setup()
    row = _promise_row()
    await repo.add(row)

    await dispatcher.tick(now=_now())

    assert await repo.list_due(now=_now(), limit=10) == []
    later = _now() + timedelta(seconds=HONESTY_REOFFENCE_RETRY_SECONDS + 1)
    assert [due.id for due in await repo.list_due(now=later, limit=10)] == [row.id]


@pytest.mark.asyncio
async def test_the_attempt_ceiling_cancels_the_row_loudly(caplog) -> None:
    turn_recorder = _RecordingTurnRecorder()
    repo, dispatcher, proactive, guard = _reoffending_setup(
        turn_recorder=turn_recorder,
    )
    row = _promise_row()
    await repo.add(row)

    when = _now()
    with caplog.at_level(logging.ERROR, logger=_DISPATCHER_LOGGER):
        for _ in range(HONESTY_PARK_ATTEMPT_LIMIT):
            assert await dispatcher.tick(now=when) == 0
            when += timedelta(seconds=HONESTY_REOFFENCE_RETRY_SECONDS + 1)

    stored = await repo.get(row.id)
    assert stored is not None
    assert stored.status == PendingFollowUpStatus.CANCELLED
    assert stored.honesty_park_attempts == HONESTY_PARK_ATTEMPT_LIMIT
    assert stored.last_error is not None
    assert proactive.calls == []

    # Never silent: an alert-line counter AND an ERROR naming the row.
    assert guard.counters.park_retries_exhausted == 1
    assert any(
        record.levelno >= logging.ERROR and row.id in record.getMessage()
        for record in caplog.records
    )
    # B-2: an ordinary promise row (not chat's own HV4 repair) giving up
    # is not a "caught lie nobody came back for" — it is just a promise
    # that could not be kept. The D6 alert line must stay clean for it.
    assert guard.counters.chat_repair_missed == 0

    # And it is terminal — a cancelled row is no longer due at any instant.
    assert await repo.list_due(now=when, limit=10) == []


@pytest.mark.asyncio
async def test_the_attempt_ceiling_on_a_repair_row_also_alerts_hv4(
    caplog,
) -> None:
    """B-2: this row is chat's OWN HV4 compensation for a lie the auditor
    already caught (F5 — minted via ``defer_reason=HONESTY_REPAIR_DEFER_REASON``).
    Cancelling it after it too keeps re-offending is the ratified, correct
    call (a compensation that keeps lying should not ship) — but D6's
    board must show that the original caught lie is now doubly unrepaired,
    not silently disappear behind ``park_retries_exhausted`` alone."""
    turn_recorder = _RecordingTurnRecorder()
    repo, dispatcher, proactive, guard = _reoffending_setup(
        turn_recorder=turn_recorder,
    )
    row = PendingFollowUp.new_promise(
        character_id="char-1",
        conversation_id="conv-1",
        promise_intent="晚點畫一張圖傳給對方",
        scheduled_for=_now() - timedelta(minutes=1),
        source_message_content="",
        turn_record_id="turn-1",
        defer_reason=HONESTY_REPAIR_DEFER_REASON,
        now=_now() - timedelta(hours=1),
    )
    await repo.add(row)

    when = _now()
    with caplog.at_level(logging.ERROR, logger=_DISPATCHER_LOGGER):
        for _ in range(HONESTY_PARK_ATTEMPT_LIMIT):
            assert await dispatcher.tick(now=when) == 0
            when += timedelta(seconds=HONESTY_REOFFENCE_RETRY_SECONDS + 1)

    stored = await repo.get(row.id)
    assert stored is not None
    assert stored.status == PendingFollowUpStatus.CANCELLED
    assert proactive.calls == []

    # Both counters move: the retry-budget alert AND the HV4 repair-missed
    # alert, because this give-up is both things at once.
    assert guard.counters.park_retries_exhausted == 1
    assert guard.counters.chat_repair_missed == 1


@pytest.mark.asyncio
async def test_the_give_up_closes_the_hv3_park_reason_chain() -> None:
    """A cancelled promise that left no audit trace would be exactly the
    silence HV exists to end, re-entered through the retry policy."""
    turn_recorder = _RecordingTurnRecorder()
    repo, dispatcher, _proactive, _guard = _reoffending_setup(
        turn_recorder=turn_recorder,
    )
    await repo.add(_promise_row())

    when = _now()
    for _ in range(HONESTY_PARK_ATTEMPT_LIMIT):
        await dispatcher.tick(now=when)
        when += timedelta(seconds=HONESTY_REOFFENCE_RETRY_SECONDS + 1)

    audits = [
        draft.post_turn_refs["outcome_claim_judge"]
        for draft in turn_recorder.drafts
    ]
    assert len(audits) == HONESTY_PARK_ATTEMPT_LIMIT
    assert [a["park_disposition"]["attempts"] for a in audits] == [1, 2, 3]
    assert [a["park_disposition"]["gave_up"] for a in audits] == [
        False, False, True,
    ]
    assert audits[-1]["park_disposition"]["kind"] == "model_reoffended"
    assert all(a["park_disposition"]["charged"] is True for a in audits)
    # The de-identification redline still holds on the new field.
    assert "傳過去" not in str(turn_recorder.drafts[-1].post_turn_refs)


@pytest.mark.asyncio
async def test_a_judge_outage_park_delays_but_never_charges_an_attempt() -> None:
    """Our own upstream failing says nothing about this promise. If it
    spent the budget, one judge outage would cancel every outstanding
    promise on the deployment — a far worse bug than the one F1 fixes."""
    guard = OutcomeClaimGuard(
        judge=_AlwaysJudge(OutcomeClaimVerdict.failed),
    )
    repo = InMemoryPendingFollowUpRepository()
    char = _character()
    proactive = _StubProactiveDispatcher()
    dispatcher = _dispatcher(
        repo=repo, character=char, composer=_LoopingPromiseComposer(),
        proactive=proactive, tool_loop=_tool_loop(guard), guard=guard,
    )
    row = _promise_row()
    await repo.add(row)

    when = _now()
    for _ in range(HONESTY_PARK_ATTEMPT_LIMIT + 3):
        assert await dispatcher.tick(now=when) == 0
        stored = await repo.get(row.id)
        assert stored is not None
        assert stored.status == PendingFollowUpStatus.QUEUED
        assert stored.honesty_park_attempts == 0
        assert stored.scheduled_for == when + timedelta(
            seconds=HONESTY_JUDGE_OUTAGE_RETRY_SECONDS,
        )
        assert stored.last_error is not None
        assert "judge_unavailable" in stored.last_error
        assert "uncharged" in stored.last_error
        when = stored.scheduled_for + timedelta(seconds=1)

    assert guard.counters.park_retries_exhausted == 0
    assert proactive.calls == []


@pytest.mark.asyncio
async def test_a_plain_empty_compose_keeps_its_pre_f1_behaviour() -> None:
    """The red line: only the honesty park changed. A composer fail-soft
    still lands back on ``queued`` still due, retried next tick."""
    repo = InMemoryPendingFollowUpRepository()
    char = _character()
    proactive = _StubProactiveDispatcher()
    composer = _SilentComposer()
    dispatcher = _dispatcher(
        repo=repo, character=char, composer=composer, proactive=proactive,
        tool_loop=None,
    )
    row = _promise_row()
    await repo.add(row)

    assert await dispatcher.tick(now=_now()) == 0

    stored = await repo.get(row.id)
    assert stored is not None
    assert stored.status == PendingFollowUpStatus.QUEUED
    assert stored.scheduled_for == row.scheduled_for
    assert stored.honesty_park_attempts == 0
    assert stored.last_error == "empty compose"
    # Still due right now — that is the whole of the untouched behaviour.
    assert [due.id for due in await repo.list_due(now=_now(), limit=10)] == [
        row.id,
    ]


@pytest.mark.asyncio
async def test_the_busy_defer_release_gets_the_same_treatment() -> None:
    """Both release methods have their own copy of the compose tail. The
    busy-defer one is the older and commoner path; a budget that only
    guarded scheduled promises would leave the original defect live on it."""
    guard = OutcomeClaimGuard(
        judge=_AlwaysJudge(lambda: OutcomeClaimVerdict.blocked(("查到了",))),
    )
    repo = InMemoryPendingFollowUpRepository()
    char = _character()
    proactive = _StubProactiveDispatcher()
    dispatcher = _dispatcher(
        repo=repo, character=char, composer=_LoopingPromiseComposer(),
        proactive=proactive, tool_loop=_tool_loop(guard), guard=guard,
        busy_composer=_LoopingBusyComposer(),
    )
    row = PendingFollowUp.new(
        character_id=char.id,
        conversation_id="conv-1",
        first_message=PendingFollowUpMessage.new(
            content="幫我查一下那個規則", queued_at=_now() - timedelta(hours=1),
        ),
        brief_reply="等等回你",
        defer_reason="會議中",
        scheduled_for=_now() - timedelta(minutes=1),
        now=_now() - timedelta(hours=1),
    )
    await repo.add(row)

    when = _now()
    for _ in range(HONESTY_PARK_ATTEMPT_LIMIT):
        assert await dispatcher.tick(now=when) == 0
        when += timedelta(seconds=HONESTY_REOFFENCE_RETRY_SECONDS + 1)

    stored = await repo.get(row.id)
    assert stored is not None
    assert stored.status == PendingFollowUpStatus.CANCELLED
    assert stored.honesty_park_attempts == HONESTY_PARK_ATTEMPT_LIMIT
    assert guard.counters.park_retries_exhausted == 1
    assert proactive.calls == []


@pytest.mark.asyncio
async def test_the_pf3_capability_park_keeps_its_deliberate_replay() -> None:
    """PF3 parks a fulfilment whose image half went to the queue, and the
    reconcile replaying it every sweep is documented, deliberate, and what
    re-mints a lost image job. Delaying or charging that park would break
    a mechanism nothing here is meant to touch."""
    guard = OutcomeClaimGuard(judge=_AlwaysJudge(OutcomeClaimVerdict.ok))
    repo = InMemoryPendingFollowUpRepository()
    char = _character()
    proactive = _StubProactiveDispatcher()
    dispatcher = _dispatcher(
        repo=repo, character=char, composer=_ToolCallingComposer(),
        proactive=proactive, tool_loop=_tool_loop(guard), guard=guard,
    )
    enqueuer = _StubCapabilityEnqueuer()
    dispatcher.set_capability_release_enqueuer(enqueuer)
    row = _promise_row()
    await repo.add(row)

    released = await dispatcher.release_row(
        row, now=_now(), defer_capabilities=True,
    )

    assert released is False
    assert enqueuer.deferred == ["image"]
    stored = await repo.get(row.id)
    assert stored is not None
    assert stored.status == PendingFollowUpStatus.QUEUED
    assert stored.scheduled_for == row.scheduled_for
    assert stored.honesty_park_attempts == 0
    assert guard.counters.park_retries_exhausted == 0
