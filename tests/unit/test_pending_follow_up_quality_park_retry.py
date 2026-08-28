"""RC — what a *quality* skip costs the row it withheld.

QG4 gave the promise seam a second gate, and routed its hard failures out
through the path this seam already had: an empty composed body, which
``_park_empty_compose`` reads as "the composer fail-softed, retry next
tick". That reading is right for a composer hiccup and wrong for this: a
composer whose prose the quality judge keeps rejecting produces the same
empty body on every tick, and each of those ticks costs two composes and
two judge calls. Nothing counted it, nothing delayed it, nothing ever
stopped it.

What this file pins:

* a quality skip moves the row's release instant forward, so the retry is
  bounded in *rate* instead of running at tick frequency;
* it charges **nothing** to the HV1 honesty budget — that budget is what
  cancels a promise outright and its ``park_retries_exhausted`` counter is
  an alert line meaning "the model kept lying". A stylistic defect must be
  unable to move either;
* the promise is never cancelled by this path (see the RC summary's
  trade-off note: the alternative needs per-row state, which needs a
  migration this ticket may not add);
* an ordinary empty compose — the pre-QG4 fail-soft — keeps its untouched
  "back to queued, still due" behaviour.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from kokoro_link.application.services.composer_tool_loop import ComposerToolLoop
from kokoro_link.application.services.outcome_claim_guard import OutcomeClaimGuard
from kokoro_link.application.services.output_quality import (
    OUTCOME_HARD_SKIPPED,
    OutputQualityCounters,
    OutputQualityOrchestrator,
)
from kokoro_link.application.services.pending_follow_up_dispatcher import (
    QUALITY_SKIP_RETRY_SECONDS,
    PendingFollowUpDispatcher,
)
from kokoro_link.contracts.novelty_gate import (
    NoveltyGateContext,
    NoveltyVerdict,
)
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
    PendingFollowUpMessage,
    PendingFollowUpStatus,
)
from kokoro_link.domain.entities.proactive_attempt import ProactiveAttempt
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.proactive_outcome import ProactiveOutcome
from kokoro_link.infrastructure.repositories.in_memory_pending_follow_ups import (
    InMemoryPendingFollowUpRepository,
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
        id=cid,
    )


def _promise_row() -> PendingFollowUp:
    return PendingFollowUp.new_promise(
        character_id="char-1",
        conversation_id="conv-1",
        promise_intent="晚點跟對方說查到的規則",
        scheduled_for=_now() - timedelta(minutes=1),
        source_message_content="等等幫我查一下",
        now=_now() - timedelta(hours=1),
    )


def _busy_row() -> PendingFollowUp:
    return PendingFollowUp.new(
        character_id="char-1",
        conversation_id="conv-1",
        first_message=PendingFollowUpMessage.new(
            content="幫我查一下那個規則", queued_at=_now() - timedelta(hours=1),
        ),
        brief_reply="等等回你",
        defer_reason="會議中",
        scheduled_for=_now() - timedelta(minutes=1),
        now=_now() - timedelta(hours=1),
    )


@dataclass
class _StubCharacterRepo:
    characters: dict[str, Character]

    async def get(self, character_id: str) -> Character | None:
        return self.characters.get(character_id)


class _StubScheduleService:
    async def ensure_schedule(self, character):
        return object()

    def resolve_current(self, schedule, *, now):
        return None, [], None


@dataclass
class _LoopingPromiseComposer(ScheduledPromiseComposerPort):
    """Writes the same defective prose on every pass, forever."""

    text: str = "查到了 </schema>"
    calls: list[ScheduledPromiseComposeInput] = field(default_factory=list)

    async def compose(self, payload):
        self.calls.append(payload)
        return ScheduledPromiseComposeOutput(content_text=self.text)


@dataclass
class _LoopingBusyComposer(PendingFollowUpComposerPort):
    text: str = "查到了 </schema>"
    calls: int = 0

    async def compose(self, payload):
        self.calls += 1
        return PendingFollowUpComposeOutput(content_text=self.text)


@dataclass
class _SilentPromiseComposer(ScheduledPromiseComposerPort):
    """The pre-QG4 composer fail-soft: empty text, no gate involved."""

    calls: int = 0

    async def compose(self, payload):
        self.calls += 1
        return ScheduledPromiseComposeOutput(content_text="")


class _StubBusyComposer(PendingFollowUpComposerPort):
    async def compose(self, payload):  # pragma: no cover - never called
        return PendingFollowUpComposeOutput(content_text="should not run")


class _AlwaysGate:
    """One verdict for every draft, every round."""

    def __init__(self, verdict_factory) -> None:  # noqa: ANN001
        self._verdict_factory = verdict_factory
        self.seen: list[NoveltyGateContext] = []

    async def evaluate(self, context, *, character=None) -> NoveltyVerdict:  # noqa: ANN001
        self.seen.append(context)
        return self._verdict_factory()


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


def _hard() -> NoveltyVerdict:
    return NoveltyVerdict(
        passes=False, structural_leak=True, feedback="混入 schema 片段",
    )


def _setup(
    *,
    verdict_factory=_hard,
    promise_composer=None,
    busy_composer=None,
    guard: OutcomeClaimGuard | None = None,
):
    gate = _AlwaysGate(verdict_factory)
    quality = OutputQualityOrchestrator(
        gate=gate, counters=OutputQualityCounters(),
    )
    repo = InMemoryPendingFollowUpRepository()
    proactive = _StubProactiveDispatcher()
    dispatcher = PendingFollowUpDispatcher(
        repository=repo,
        composer=busy_composer or _StubBusyComposer(),
        proactive_dispatcher=proactive,
        character_repository=_StubCharacterRepo({"char-1": _character()}),
        schedule_service=_StubScheduleService(),
        scheduled_promise_composer=promise_composer or _LoopingPromiseComposer(),
        tool_loop=ComposerToolLoop(output_quality_orchestrator=quality),
        outcome_claim_guard=guard,
    )
    return repo, dispatcher, proactive, quality, guard


@pytest.mark.asyncio
async def test_a_quality_skip_delays_the_next_attempt() -> None:
    repo, dispatcher, proactive, quality, _guard = _setup()
    row = _promise_row()
    await repo.add(row)

    assert await dispatcher.tick(now=_now()) == 0
    assert proactive.calls == []

    stored = await repo.get(row.id)
    assert stored is not None
    assert stored.status == PendingFollowUpStatus.QUEUED
    assert stored.scheduled_for == _now() + timedelta(
        seconds=QUALITY_SKIP_RETRY_SECONDS,
    )
    assert stored.last_error is not None
    assert "quality" in stored.last_error
    assert quality.counters.total("promise", OUTCOME_HARD_SKIPPED) == 1
    # And the row stops holding the head of the due queue in the meantime.
    assert await repo.list_due(now=_now(), limit=10) == []


@pytest.mark.asyncio
async def test_a_quality_skip_never_charges_the_honesty_budget() -> None:
    """The alert line ``park_retries_exhausted`` means "the model kept
    lying". A draft that was merely unreadable must not be able to ring it,
    nor to spend the budget that cancels the promise."""
    guard = OutcomeClaimGuard(judge=_UnusedJudge())
    repo, dispatcher, proactive, _quality, _guard = _setup(guard=guard)
    row = _promise_row()
    await repo.add(row)

    when = _now()
    for _ in range(5):
        assert await dispatcher.tick(now=when) == 0
        stored = await repo.get(row.id)
        assert stored is not None
        assert stored.status == PendingFollowUpStatus.QUEUED
        assert stored.honesty_park_attempts == 0
        when = stored.scheduled_for + timedelta(seconds=1)

    assert guard.counters.park_retries_exhausted == 0
    assert guard.counters.parked == 0
    assert proactive.calls == []


class _UnusedJudge:
    async def judge(self, **_kwargs) -> OutcomeClaimVerdict:  # pragma: no cover
        raise AssertionError("the honesty judge must not see a withheld draft")


@pytest.mark.asyncio
async def test_the_busy_defer_release_gets_the_same_treatment() -> None:
    """Both release methods carry their own copy of the compose tail."""
    repo, dispatcher, proactive, _quality, _guard = _setup(
        busy_composer=_LoopingBusyComposer(),
    )
    row = _busy_row()
    await repo.add(row)

    assert await dispatcher.tick(now=_now()) == 0

    stored = await repo.get(row.id)
    assert stored is not None
    assert stored.status == PendingFollowUpStatus.QUEUED
    assert stored.scheduled_for == _now() + timedelta(
        seconds=QUALITY_SKIP_RETRY_SECONDS,
    )
    assert stored.honesty_park_attempts == 0
    assert proactive.calls == []


@pytest.mark.asyncio
async def test_a_plain_empty_compose_keeps_its_pre_qg4_behaviour() -> None:
    """The red line: only the *quality-withheld* empty body changed."""
    composer = _SilentPromiseComposer()
    repo, dispatcher, _proactive, _quality, _guard = _setup(
        promise_composer=composer,
    )
    row = _promise_row()
    await repo.add(row)

    assert await dispatcher.tick(now=_now()) == 0

    stored = await repo.get(row.id)
    assert stored is not None
    assert stored.status == PendingFollowUpStatus.QUEUED
    assert stored.scheduled_for == row.scheduled_for
    assert stored.last_error == "empty compose"
    assert [due.id for due in await repo.list_due(now=_now(), limit=10)] == [
        row.id,
    ]


@pytest.mark.asyncio
async def test_a_broken_quality_judge_does_not_park_anything() -> None:
    """Fail-open is not a skip. A judge that cannot answer lets the draft
    through unreviewed (that is the whole point of the direction it fails
    in), so there is no withheld round to back off — and nothing to charge,
    the same rule the honesty gate applies to its own outage."""

    def _boom() -> NoveltyVerdict:
        raise RuntimeError("judge route down")

    repo, dispatcher, proactive, quality, _guard = _setup(
        verdict_factory=_boom,
        promise_composer=_LoopingPromiseComposer(text="規則我查完了，等等說給你聽"),
    )
    row = _promise_row()
    await repo.add(row)

    assert await dispatcher.tick(now=_now()) == 1

    stored = await repo.get(row.id)
    assert stored is not None
    assert stored.status == PendingFollowUpStatus.RESOLVED
    assert [call["text"] for call in proactive.calls] == [
        "規則我查完了，等等說給你聽",
    ]
    assert quality.counters.total("promise", OUTCOME_HARD_SKIPPED) == 0


@pytest.mark.asyncio
async def test_a_passing_verdict_delivers_exactly_as_before() -> None:
    repo, dispatcher, proactive, _quality, _guard = _setup(
        verdict_factory=lambda: NoveltyVerdict(passes=True),
        promise_composer=_LoopingPromiseComposer(text="規則我查完了，等等說給你聽"),
    )
    row = _promise_row()
    await repo.add(row)

    assert await dispatcher.tick(now=_now()) == 1

    stored = await repo.get(row.id)
    assert stored is not None
    assert stored.status == PendingFollowUpStatus.RESOLVED
    assert [call["text"] for call in proactive.calls] == [
        "規則我查完了，等等說給你聽",
    ]
