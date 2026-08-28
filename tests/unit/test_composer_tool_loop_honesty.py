"""HV1 — the honesty gate on both exits of ``ComposerToolLoop.run``.

The defect these pin is the one seen in production on 2026-08-25: pass 1
answers in fluent prose claiming a photo was drawn and attached, calls no
tool at all, and the message ships with ``attachments=[]``. The composer's
own guard cannot see it (the sentence is not machine output), and a
keyword table is forbidden — so the gate is a semantic judge, stubbed
here so the tests assert the *loop's* reaction rather than a model's
opinion.

Also pinned here: the D5 fact about what happens after a park. The plan
allowed for the retry cadence needing an explicit ``KnobGate.NONE``
exemption; the registry already gives it one, so the exemption is a
fact to hold still rather than code to write.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone

import pytest

import kokoro_link.application.services.composer_tool_loop as composer_tool_loop_module
from kokoro_link.application.services.composer_tool_loop import (
    DELIVERED_WITHOUT_TEXT_FALLBACK_KEY,
    UNDELIVERABLE_ARTIFACT_FALLBACK_KEY,
    ComposerToolLoop,
)
from kokoro_link.application.services.outcome_claim_guard import (
    OutcomeClaimGuard,
)
from kokoro_link.application.services.tool_orchestrator import ToolOrchestrator
from kokoro_link.contracts.due_jobs import (
    PENDING_FOLLOW_UP_IMAGE_RELEASE_KIND,
    PENDING_FOLLOW_UP_RELEASE_KIND,
    KnobGate,
    kind_spec,
)
from kokoro_link.contracts.outcome_claim import (
    OutcomeClaimEvidence,
    OutcomeClaimVerdict,
)
from kokoro_link.contracts.scheduled_promise_composer import (
    ScheduledPromiseComposeInput,
    ScheduledPromiseComposeOutput,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.tool_call import ToolCall
from kokoro_link.infrastructure.localization.fallback_texts import (
    localized_fallback_text,
)
from kokoro_link.infrastructure.repositories.in_memory_tool_invocations import (
    InMemoryToolInvocationRepository,
)
from kokoro_link.infrastructure.tools.fake_tools import FakeImageTool
from kokoro_link.infrastructure.tools.registry import InMemoryToolRegistry


def _now() -> datetime:
    return datetime(2026, 8, 25, 9, 36, tzinfo=timezone.utc)


def _character() -> Character:
    return replace(
        Character.create(
            name="Aki", summary="", personality=[], interests=[],
            speaking_style="", boundaries=[],
            state=CharacterState(
                emotion="neutral", affection=50, fatigue=20, trust=50,
                energy=70,
            ),
        ),
        id="char-1",
        allowed_tools=["fake_image"],
    )


def _payload() -> ScheduledPromiseComposeInput:
    return ScheduledPromiseComposeInput(
        character=_character(),
        promise_intent="晚點畫一張圖傳給對方",
        promise_text="等等畫一張給我看",
        scheduled_for=_now(),
        current_activity=None,
        just_finished_activity=None,
        recent_dialogue_summary=None,
        now=_now(),
    )


# --------------------------------------------------------------------
# Stubs
# --------------------------------------------------------------------


@dataclass
class _ScriptedComposer:
    """Answers each compose call from a script, recording what it saw."""

    script: list[ScheduledPromiseComposeOutput]
    seen: list[ScheduledPromiseComposeInput] = field(default_factory=list)

    async def compose(
        self, payload: ScheduledPromiseComposeInput,
    ) -> ScheduledPromiseComposeOutput:
        self.seen.append(payload)
        if not self.script:  # pragma: no cover - a test asked for too many
            raise AssertionError("composer called more times than scripted")
        return self.script.pop(0)


@dataclass
class _ScriptedJudge:
    """A stub verdict source. The real judge is a model; the loop's job
    is to react to the verdict, which is what these tests assert."""

    verdicts: list[OutcomeClaimVerdict]
    seen: list[tuple[str, OutcomeClaimEvidence]] = field(default_factory=list)

    async def judge(
        self,
        *,
        message_text: str,
        evidence: OutcomeClaimEvidence,
        character: Character | None = None,
        operator_primary_language: str = "",
    ) -> OutcomeClaimVerdict:
        self.seen.append((message_text, evidence))
        if not self.verdicts:  # pragma: no cover
            raise AssertionError("judge called more times than scripted")
        return self.verdicts.pop(0)


class _RaisingJudge:
    async def judge(self, **_kwargs) -> OutcomeClaimVerdict:
        raise RuntimeError("judge upstream is down")


def _loop(
    *,
    guard: OutcomeClaimGuard | None,
    public_base_url: str = "https://yura.example",
) -> ComposerToolLoop:
    registry = InMemoryToolRegistry([FakeImageTool()])
    return ComposerToolLoop(
        tool_registry=registry,
        tool_orchestrator=ToolOrchestrator(
            registry=registry,
            invocation_repository=InMemoryToolInvocationRepository(),
        ),
        public_base_url=public_base_url,
        outcome_claim_guard=guard,
    )


def _prose(text: str) -> ScheduledPromiseComposeOutput:
    return ScheduledPromiseComposeOutput(content_text=text)


def _call() -> ScheduledPromiseComposeOutput:
    return ScheduledPromiseComposeOutput(
        content_text="",
        tool_calls=(ToolCall(name="fake_image", arguments={"scene": "廚房"}),),
    )


# --------------------------------------------------------------------
# The zero-call exit
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_zero_call_outcome_claim_never_ships() -> None:
    """The 2026-08-25 shape: claimed a picture, called nothing.

    Two offences in a row (the correction pass repeats the claim) must
    end with an empty body — the composers' "retry next tick" — and never
    with the claim reaching the player.
    """
    composer = _ScriptedComposer([
        _prose("畫好囉！圖片附上～"),
        _prose("真的畫好了，這次一定有附上"),
    ])
    judge = _ScriptedJudge([
        OutcomeClaimVerdict.blocked(("圖片附上",)),
        OutcomeClaimVerdict.blocked(("這次一定有附上",)),
    ])
    guard = OutcomeClaimGuard(judge=judge)

    result = await _loop(guard=guard).run(
        character=_character(), payload=_payload(), compose=composer.compose,
    )

    assert result.content_text == ""
    assert result.attachments == ()
    assert guard.counters.blocked_zero_call == 1
    assert guard.counters.parked == 1
    # The correction re-run happened exactly once — no third attempt.
    assert len(composer.seen) == 2


@pytest.mark.asyncio
async def test_zero_call_correction_reaches_the_composer() -> None:
    """The retry must actually carry the correction, and only the retry.

    A re-compose with the identical prompt is a wasted call whose outcome
    is already known, so the field being populated on pass 2 (and empty on
    pass 1) is the thing that makes the retry meaningful."""
    composer = _ScriptedComposer([
        _prose("查到了！我幫你看過了"),
        _prose("我等等幫你查，晚點跟你說"),
    ])
    guard = OutcomeClaimGuard(judge=_ScriptedJudge([
        OutcomeClaimVerdict.blocked(("查到了",)),
        OutcomeClaimVerdict.ok(),
    ]))

    result = await _loop(guard=guard).run(
        character=_character(), payload=_payload(), compose=composer.compose,
    )

    assert result.content_text == "我等等幫你查，晚點跟你說"
    assert composer.seen[0].honesty_correction == ""
    correction = composer.seen[1].honesty_correction
    assert "查到了" in correction
    # Both honest ways out are offered; dropping the "call the tool"
    # branch would turn every promised photo into an apology.
    assert "工具 JSON" in correction
    assert guard.counters.corrected == 1
    assert guard.counters.parked == 0


# --------------------------------------------------------------------
# B-5: MAX_HONESTY_RETRIES actually bounds the correction loop
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_max_honesty_retries_governs_how_many_zero_call_corrections_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Raising the constant must raise the retry count for real.

    At the shipped value of 1 this shape is indistinguishable from a
    hardcoded single retry (see ``test_zero_call_outcome_claim_never_ships``)
    — the only way to prove the loop actually reads
    ``MAX_HONESTY_RETRIES``, rather than the value merely sitting next to
    unrelated code, is to change it and watch the call count follow."""
    monkeypatch.setattr(composer_tool_loop_module, "MAX_HONESTY_RETRIES", 2)
    composer = _ScriptedComposer([
        _prose("畫好囉！圖片附上～"),
        _prose("這次真的附上了"),
        _prose("我等等畫給你看"),
    ])
    guard = OutcomeClaimGuard(judge=_ScriptedJudge([
        OutcomeClaimVerdict.blocked(("圖片附上",)),
        OutcomeClaimVerdict.blocked(("這次真的附上了",)),
        OutcomeClaimVerdict.ok(),
    ]))

    result = await _loop(guard=guard).run(
        character=_character(), payload=_payload(), compose=composer.compose,
    )

    # Two offences no longer end the round: with the bound raised to 2,
    # a second correction attempt runs and succeeds.
    assert result.content_text == "我等等畫給你看"
    assert len(composer.seen) == 3
    assert guard.counters.corrected == 1
    assert guard.counters.parked == 0
    # Each failed attempt is its own blocked event (initial + one retry
    # that reoffended), and the second correction is built from the
    # *second* verdict's claims, not a stale copy of the first.
    assert guard.counters.blocked_zero_call == 2
    assert "圖片附上" in composer.seen[1].honesty_correction
    assert "這次真的附上了" in composer.seen[2].honesty_correction


@pytest.mark.asyncio
async def test_max_honesty_retries_still_parks_once_the_bound_is_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The raised bound is a ceiling, not a licence to retry forever."""
    monkeypatch.setattr(composer_tool_loop_module, "MAX_HONESTY_RETRIES", 2)
    composer = _ScriptedComposer([
        _prose("畫好囉！圖片附上～"),
        _prose("這次真的附上了"),
        _prose("真的真的附上了"),
    ])
    guard = OutcomeClaimGuard(judge=_ScriptedJudge([
        OutcomeClaimVerdict.blocked(("圖片附上",)),
        OutcomeClaimVerdict.blocked(("這次真的附上了",)),
        OutcomeClaimVerdict.blocked(("真的真的附上了",)),
    ]))

    result = await _loop(guard=guard).run(
        character=_character(), payload=_payload(), compose=composer.compose,
    )

    assert result.content_text == ""
    assert guard.counters.parked == 1
    # Exactly 2 retries, not a 3rd — the bound this test raised, not an
    # unbounded loop.
    assert len(composer.seen) == 3


@pytest.mark.asyncio
async def test_consistent_zero_call_prose_ships_untouched() -> None:
    """A promise about later is not a claim about now.

    The gate must cost nothing on the overwhelmingly common case: one
    compose, one verdict, the text out as written."""
    composer = _ScriptedComposer([_prose("晚點畫好傳給你！")])
    guard = OutcomeClaimGuard(judge=_ScriptedJudge([OutcomeClaimVerdict.ok()]))

    result = await _loop(guard=guard).run(
        character=_character(), payload=_payload(), compose=composer.compose,
    )

    assert result.content_text == "晚點畫好傳給你！"
    assert len(composer.seen) == 1
    assert guard.counters.consistent == 1


@pytest.mark.asyncio
async def test_correction_may_be_answered_with_a_real_tool_call() -> None:
    """The honest road the loop must leave open.

    Told "either call the tool or stop claiming", a model that picks the
    first must land in the ordinary tool path — the picture is rendered
    and shipped — not be treated as another offence."""
    composer = _ScriptedComposer([
        _prose("圖畫好了，附上！"),
        _call(),
        _prose("畫好了，你看看喜不喜歡"),
    ])
    guard = OutcomeClaimGuard(judge=_ScriptedJudge([
        OutcomeClaimVerdict.blocked(("附上",)),
        OutcomeClaimVerdict.ok(),
    ]))

    result = await _loop(guard=guard).run(
        character=_character(), payload=_payload(), compose=composer.compose,
    )

    assert result.content_text == "畫好了，你看看喜不喜歡"
    assert len(result.attachments) == 1
    assert guard.counters.corrected == 1


@pytest.mark.asyncio
async def test_judge_failure_parks_rather_than_shipping() -> None:
    """Fail-closed: no verdict is not an approval.

    A crashing judge must withhold the message, not wave it through —
    fail-open here would reopen the whole hole the gate exists to close.
    """
    composer = _ScriptedComposer([_prose("圖片已經傳過去囉")])
    guard = OutcomeClaimGuard(judge=_RaisingJudge())

    result = await _loop(guard=guard).run(
        character=_character(), payload=_payload(), compose=composer.compose,
    )

    assert result.content_text == ""
    assert guard.counters.judge_failed == 1
    assert guard.counters.parked == 1
    # One failure is weather, not an outage.
    assert guard.counters.judge_outage == 0


@pytest.mark.asyncio
async def test_consecutive_judge_failures_raise_the_outage_alarm(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Silence is this gate's failure mode — three in a row must be loud."""
    guard = OutcomeClaimGuard(judge=_RaisingJudge(), failure_alarm_streak=3)
    loop = _loop(guard=guard)

    with caplog.at_level("ERROR"):
        for _ in range(3):
            composer = _ScriptedComposer([_prose("查好了")])
            await loop.run(
                character=_character(),
                payload=_payload(),
                compose=composer.compose,
            )

    assert guard.counters.judge_failed == 3
    assert guard.counters.judge_outage == 1
    assert any("consecutive judge failures" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_one_success_resets_the_failure_streak() -> None:
    """An intermittent failure must not accumulate into a fake outage."""

    class _Flaky:
        def __init__(self) -> None:
            self.calls = 0

        async def judge(self, **_kwargs) -> OutcomeClaimVerdict:
            self.calls += 1
            if self.calls == 2:
                return OutcomeClaimVerdict.ok()
            return OutcomeClaimVerdict.failed()

    guard = OutcomeClaimGuard(judge=_Flaky(), failure_alarm_streak=3)
    loop = _loop(guard=guard)
    for _ in range(4):
        composer = _ScriptedComposer([_prose("晚點傳給你")])
        await loop.run(
            character=_character(),
            payload=_payload(),
            compose=composer.compose,
        )

    assert guard.counters.judge_failed == 3
    assert guard.counters.judge_outage == 0


@pytest.mark.asyncio
async def test_no_guard_leaves_the_pre_hv1_behaviour_byte_for_byte() -> None:
    """A deployment with no judge route keeps exactly what it had."""
    composer = _ScriptedComposer([_prose("畫好了，附上圖片")])

    result = await _loop(guard=None).run(
        character=_character(), payload=_payload(), compose=composer.compose,
    )

    assert result.content_text == "畫好了，附上圖片"
    assert len(composer.seen) == 1


# --------------------------------------------------------------------
# The pass-2 exit
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pass_two_overclaim_is_re_composed_once_then_shipped() -> None:
    """A tool ran; the prose overstated it; the retry is honest."""
    composer = _ScriptedComposer([
        _call(),
        _prose("查到超多資料，全部整理好了"),
        _prose("圖畫好了，附在這則訊息裡"),
    ])
    judge = _ScriptedJudge([
        OutcomeClaimVerdict.blocked(("查到超多資料",)),
        OutcomeClaimVerdict.ok(),
    ])
    guard = OutcomeClaimGuard(judge=judge)

    result = await _loop(guard=guard).run(
        character=_character(), payload=_payload(), compose=composer.compose,
    )

    assert result.content_text == "圖畫好了，附在這則訊息裡"
    assert len(result.attachments) == 1
    assert guard.counters.blocked_after_tools == 1
    assert guard.counters.corrected == 1
    # The judge saw the *delivered* attachment count, not the produced one.
    assert judge.seen[0][1].delivered_attachments == 1
    assert judge.seen[0][1].outcomes[0].tool_name == "fake_image"


@pytest.mark.asyncio
async def test_pass_two_second_offence_keeps_the_produced_artifact() -> None:
    """A dishonest sentence is no reason to re-render a picture forever.

    With no public base URL the render succeeded but nothing can carry
    it, so the PF three-state fallback answers the promise in words —
    the one branch where "park and retry" would loop a GPU on every
    reconcile."""
    composer = _ScriptedComposer([
        _call(),
        _prose("照片傳過去了！"),
        _prose("照片真的傳過去了！"),
    ])
    guard = OutcomeClaimGuard(judge=_ScriptedJudge([
        OutcomeClaimVerdict.blocked(("照片傳過去了",)),
        OutcomeClaimVerdict.blocked(("照片真的傳過去了",)),
    ]))

    result = await _loop(guard=guard, public_base_url="").run(
        character=_character(), payload=_payload(), compose=composer.compose,
    )

    assert result.content_text == localized_fallback_text(
        UNDELIVERABLE_ARTIFACT_FALLBACK_KEY, "",
    )
    assert result.attachments == ()
    assert guard.counters.parked == 1


@pytest.mark.asyncio
async def test_pass_two_second_offence_with_deliverable_attachments_is_not_parked() -> None:
    """S7. Same second-offence shape as the test above, but WITH a public
    base URL — the tool's render is deliverable this time, so
    ``_no_final_text`` ships it with the fixed fallback line instead of
    sending nothing. That is a delivered round, not a parked one: the
    honesty gate withheld the *prose* twice (one initial block, one
    failed correction), never the attachment, and ``parked`` must reflect
    that nothing was actually withheld from the player."""
    composer = _ScriptedComposer([
        _call(),
        _prose("照片傳過去了！"),
        _prose("照片真的傳過去了！"),
    ])
    guard = OutcomeClaimGuard(judge=_ScriptedJudge([
        OutcomeClaimVerdict.blocked(("照片傳過去了",)),
        OutcomeClaimVerdict.blocked(("照片真的傳過去了",)),
    ]))

    result = await _loop(guard=guard).run(
        character=_character(), payload=_payload(), compose=composer.compose,
    )

    assert result.content_text == localized_fallback_text(
        DELIVERED_WITHOUT_TEXT_FALLBACK_KEY, "",
    )
    assert len(result.attachments) == 1
    # The whole point: not counted as a park (nothing was withheld from
    # the player), and the single dishonesty this round is still counted
    # exactly once — not twice, once from the initial block and again
    # from the park call that followed the failed correction.
    assert guard.counters.parked == 0
    assert guard.counters.blocked_after_tools == 1


@pytest.mark.asyncio
async def test_pass_two_judge_failure_fails_closed() -> None:
    """No verdict after a tool ran: withhold, keep the artifact."""
    composer = _ScriptedComposer([_call(), _prose("圖來囉")])
    guard = OutcomeClaimGuard(judge=_RaisingJudge())

    result = await _loop(guard=guard).run(
        character=_character(), payload=_payload(), compose=composer.compose,
    )

    # Deliverable artifact + refused prose → the shared "delivered without
    # text" fallback ships the picture rather than re-rendering it.
    assert result.content_text != "圖來囉"
    assert len(result.attachments) == 1
    assert guard.counters.judge_failed == 1
    # S7: a judge outage that still ships a deliverable attachment is not
    # "the round sent nothing" — it must not read as a park, and since no
    # verdict was ever found inconsistent this round, the delivered-but-
    # blocked fact is recorded fresh rather than assumed already counted.
    assert guard.counters.parked == 0
    assert guard.counters.blocked_after_tools == 1


# --------------------------------------------------------------------
# D5 — what a parked promise's retry is scaled by
# --------------------------------------------------------------------


def test_parked_promise_retry_is_not_scaled_by_the_tier_background_knob() -> None:
    """D5, discharged by the registry rather than by new code.

    A park means "本該送到卻沒送到"; stretching its retry by the account's
    background multiplier would make a low-tier player wait longest for a
    message they were already owed. Both release kinds are already
    ``KnobGate.NONE`` — the multiplier resolver returns 1 for those
    without ever reading a profile — so the exemption D5 asks for exists.
    This test is what stops someone "tidying" them onto BACKGROUND with
    the rest of the promise machinery.
    """
    for kind in (
        PENDING_FOLLOW_UP_RELEASE_KIND,
        PENDING_FOLLOW_UP_IMAGE_RELEASE_KIND,
    ):
        spec = kind_spec(kind)
        assert spec is not None
        assert spec.knob_gate is KnobGate.NONE, kind
        # One-shot, event-driven: the retry is a fresh job minted by the
        # reconcile sweep, not a self-chain whose interval could drift.
        assert spec.chained is False, kind
        assert spec.event_driven is True, kind


def test_release_kinds_are_not_dormancy_exempt() -> None:
    """真休眠不豁免 — the D5 exemption is about cadence, not about waking
    a character nobody has spoken to in a week. Neither release kind
    claims ``dormancy_exempt``, and this pins that the honesty retry did
    not quietly acquire it."""
    for kind in (
        PENDING_FOLLOW_UP_RELEASE_KIND,
        PENDING_FOLLOW_UP_IMAGE_RELEASE_KIND,
    ):
        assert kind_spec(kind).dormancy_exempt is False, kind
