"""QG4 — the player-visible quality band on ``ComposerToolLoop``.

Before this, the follow-up / scheduled-promise seam had exactly one gate
and it asked one question: *is this message true?* Nothing asked whether
it was **readable** — a reply that leaked a schema fragment, stopped
mid-sentence at the composer's 600/800-character cut, or answered a
繁體中文 player in English was perfectly honest and shipped anyway.

What these tests pin is the *placement*, because that is the part a later
refactor can silently get wrong:

* the quality band reviews the **same final prose** the honesty gate is
  about to review, and it runs **first** (so the honesty judge sees
  whatever quality regenerated);
* a hard failure that survives its one regeneration ends the round the
  way this seam already ends rounds — an empty body, which the dispatcher
  reads as "retry next tick" (``_park_empty_compose`` with no honesty
  disposition), **not** as an honesty park;
* the fixed localized fallback lines are never reviewed — they are
  constants, and a gate opinion about a constant is a model call spent on
  nothing;
* the honesty gate's own re-compose does **not** come back through the
  quality band (accepted residual R-QG-2).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone

import pytest

from kokoro_link.application.services.composer_tool_loop import (
    DELIVERED_WITHOUT_TEXT_FALLBACK_KEY,
    SURFACE_FOLLOW_UP,
    SURFACE_PROMISE,
    ComposerToolLoop,
)
from kokoro_link.application.services.outcome_claim_guard import (
    OutcomeClaimGuard,
)
from kokoro_link.application.services.output_quality import (
    OUTCOME_HARD_SKIPPED,
    OUTCOME_PASS,
    OutputQualityOrchestrator,
)
from kokoro_link.application.services.tool_orchestrator import ToolOrchestrator
from kokoro_link.contracts.novelty_gate import (
    NoveltyGateContext,
    NoveltyVerdict,
)
from kokoro_link.contracts.outcome_claim import (
    OutcomeClaimEvidence,
    OutcomeClaimVerdict,
)
from kokoro_link.contracts.pending_follow_up_composer import (
    PendingFollowUpComposeInput,
)
from kokoro_link.contracts.scheduled_promise_composer import (
    ScheduledPromiseComposeInput,
    ScheduledPromiseComposeOutput,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.conversation import MessageContentMode
from kokoro_link.domain.entities.pending_follow_up import PendingFollowUpMessage
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.tool_call import ToolCall
from kokoro_link.infrastructure.localization.fallback_texts import (
    localized_fallback_text,
)
from kokoro_link.infrastructure.repositories.in_memory_tool_invocations import (
    InMemoryToolInvocationRepository,
)
from kokoro_link.infrastructure.tools.fake_tools import EchoTool, FakeImageTool
from kokoro_link.infrastructure.tools.registry import InMemoryToolRegistry


def _now() -> datetime:
    return datetime(2026, 8, 26, 9, 36, tzinfo=timezone.utc)


def _character(*, allowed: tuple[str, ...] = ("fake_image",)) -> Character:
    return replace(
        Character.create(
            name="Aki", summary="", personality=["溫和"], interests=[],
            speaking_style="平鋪直敘", boundaries=[],
            state=CharacterState(
                emotion="neutral", affection=50, fatigue=20, trust=50,
                energy=70,
            ),
        ),
        id="char-1",
        user_id="op-1",
        allowed_tools=list(allowed),
    )


def _payload() -> ScheduledPromiseComposeInput:
    return ScheduledPromiseComposeInput(
        character=_character(),
        promise_intent="晚點畫一張圖傳給對方",
        promise_text="等等畫一張給我看",
        scheduled_for=_now(),
        current_activity=None,
        just_finished_activity=None,
        recent_dialogue_summary="剛聊完新開的咖啡廳",
        now=_now(),
    )


def _follow_up_payload() -> PendingFollowUpComposeInput:
    return PendingFollowUpComposeInput(
        character=_character(),
        queued_messages=(
            PendingFollowUpMessage.new(content="你在忙嗎", queued_at=_now()),
            PendingFollowUpMessage.new(content="那個規則到底怎麼算", queued_at=_now()),
        ),
        brief_reply="等開完會我再仔細回你",
        defer_reason="會議中",
        queued_at=_now(),
        just_finished_activity=None,
        current_activity=None,
        recent_dialogue_summary="上週聊過規則書",
        now=_now(),
    )


# --------------------------------------------------------------------
# Stubs
# --------------------------------------------------------------------


@dataclass
class _ScriptedComposer:
    script: list[ScheduledPromiseComposeOutput]
    seen: list[ScheduledPromiseComposeInput] = field(default_factory=list)

    async def compose(self, payload):  # noqa: ANN001, ANN201
        self.seen.append(payload)
        if not self.script:  # pragma: no cover - a test asked for too many
            raise AssertionError("composer called more times than scripted")
        return self.script.pop(0)


@dataclass
class _ScriptedQualityGate:
    """A stub verdict source for the nine-axis judge.

    The real gate is a model; these tests assert the *loop's* reaction to
    a verdict, which is the only part this repo owns.
    """

    verdicts: list[NoveltyVerdict]
    seen: list[NoveltyGateContext] = field(default_factory=list)

    async def evaluate(
        self, context: NoveltyGateContext, *, character: Character | None = None,
    ) -> NoveltyVerdict:
        self.seen.append(context)
        if not self.verdicts:  # pragma: no cover
            raise AssertionError("quality gate called more times than scripted")
        return self.verdicts.pop(0)


@dataclass
class _ScriptedJudge:
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


def _hard(feedback: str = "洩漏了結構標記") -> NoveltyVerdict:
    return NoveltyVerdict(passes=False, structural_leak=True, feedback=feedback)


def _soft(feedback: str = "太像上一則") -> NoveltyVerdict:
    return NoveltyVerdict(passes=False, lacks_novelty=True, feedback=feedback)


def _ok() -> NoveltyVerdict:
    return NoveltyVerdict(passes=True)


def _loop(
    *,
    quality: OutputQualityOrchestrator | None = None,
    guard: OutcomeClaimGuard | None = None,
    tools: tuple = (),
    public_base_url: str = "https://yura.example",
) -> ComposerToolLoop:
    registry = InMemoryToolRegistry(list(tools)) if tools else None
    return ComposerToolLoop(
        tool_registry=registry,
        tool_orchestrator=(
            ToolOrchestrator(
                registry=registry,
                invocation_repository=InMemoryToolInvocationRepository(),
            )
            if registry is not None
            else None
        ),
        public_base_url=public_base_url,
        outcome_claim_guard=guard,
        output_quality_orchestrator=quality,
    )


def _prose(text: str) -> ScheduledPromiseComposeOutput:
    return ScheduledPromiseComposeOutput(content_text=text)


def _call(name: str = "fake_image") -> ScheduledPromiseComposeOutput:
    return ScheduledPromiseComposeOutput(
        content_text="",
        tool_calls=(ToolCall(name=name, arguments={"scene": "廚房"}),),
    )


# --------------------------------------------------------------------
# The no-tool exit — the only exit a self-host without a registry has
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_quality_gate_unwired_leaves_the_plain_compose_untouched() -> None:
    """No orchestrator → exactly one compose, byte-for-byte as before."""
    composer = _ScriptedComposer([_prose("我回來了，剛開完會")])

    result = await _loop().run(
        character=_character(), payload=_payload(), compose=composer.compose,
    )

    assert result.content_text == "我回來了，剛開完會"
    assert len(composer.seen) == 1


@pytest.mark.asyncio
async def test_no_tool_exit_is_gated_and_a_surviving_hard_failure_sends_nothing(
) -> None:
    """A deployment with no tool registry still gets the quality band.

    This is the *only* exit a self-host without tools ever takes, so
    leaving it ungated would mean the surface named in the plan is gated
    on hosted and naked everywhere else.
    """
    composer = _ScriptedComposer([
        _prose("我回來了 </response> 剛開完"),
        _prose("我回來了 </response> 真的剛開完"),
    ])
    gate = _ScriptedQualityGate([_hard(), _hard()])
    quality = OutputQualityOrchestrator(gate=gate)

    result = await _loop(quality=quality).run(
        character=_character(), payload=_payload(), compose=composer.compose,
    )

    assert result.content_text == ""
    assert result.attachments == ()
    # One regeneration, then it stops — the D1 background row.
    assert len(composer.seen) == 2
    assert quality.counters.total("promise", OUTCOME_HARD_SKIPPED) == 1


@pytest.mark.asyncio
async def test_quality_correction_reaches_the_composer_only_on_the_retry() -> None:
    """The retry must carry the judge's feedback, and only the retry.

    A re-compose with the identical prompt is a call whose outcome is
    already known; the correction is what makes the second draft mean
    something.
    """
    composer = _ScriptedComposer([
        _prose("嗨嗨嗨"),
        _prose("剛開完會，那個規則我等等查給你"),
    ])
    gate = _ScriptedQualityGate([_hard("結尾斷句，且混入標記"), _ok()])

    result = await _loop(quality=OutputQualityOrchestrator(gate=gate)).run(
        character=_character(), payload=_payload(), compose=composer.compose,
    )

    assert result.content_text == "剛開完會，那個規則我等等查給你"
    assert composer.seen[0].honesty_correction == ""
    correction = composer.seen[1].honesty_correction
    assert "結尾斷句，且混入標記" in correction


@pytest.mark.asyncio
async def test_soft_failure_never_withholds_the_round() -> None:
    """Soft axes are opinions; a whole missing message costs more.

    Note *which* draft ships when the re-review is still soft: the shared
    band keeps the regeneration, not the original. It was written in
    response to the feedback, so it is the likelier of the two to be
    better, and the caller does not get a vote — that decision lives in
    the D1 table once, for every surface.
    """
    composer = _ScriptedComposer([
        _prose("今天過得還可以"),
        _prose("今天過得還可以，剛剛在陽台看雨"),
    ])
    gate = _ScriptedQualityGate([_soft(), _soft()])

    result = await _loop(quality=OutputQualityOrchestrator(gate=gate)).run(
        character=_character(), payload=_payload(), compose=composer.compose,
    )

    assert result.content_text == "今天過得還可以，剛剛在陽台看雨"
    assert len(composer.seen) == 2


@pytest.mark.asyncio
async def test_nsfw_queued_text_never_reaches_a_frontier_review() -> None:
    """The judge call is routed like any other, so it obeys the same rule.

    Text captured while the conversation was in NSFW mode is replaced by
    its frontier-safe summary — exactly the substitution the composer
    prompt makes — because a frontier provider is what
    ``content_tolerance`` routes this review to.
    """
    payload = replace(
        _follow_up_payload(),
        queued_messages=(
            PendingFollowUpMessage.new(
                content="露骨原文",
                queued_at=_now(),
                content_mode=MessageContentMode.NSFW,
                safe_summary="（親密話題）",
            ),
        ),
    )
    composer = _ScriptedComposer([_prose("我回來了")])
    gate = _ScriptedQualityGate([_ok()])

    await _loop(quality=OutputQualityOrchestrator(gate=gate)).run(
        character=_character(), payload=payload, compose=composer.compose,
    )

    context = gate.seen[0]
    assert context.content_tolerance == "frontier"
    assert context.latest_user_message == "（親密話題）"
    assert "露骨原文" not in "\n".join(context.known_material)


@pytest.mark.asyncio
async def test_gate_context_carries_the_candidate_and_the_compose_material(
) -> None:
    """The judge is handed the prose being reviewed and what it was
    written from — not an empty shell that makes every axis unanswerable.
    """
    composer = _ScriptedComposer([_prose("那個規則我查完了會跟你說")])
    gate = _ScriptedQualityGate([_ok()])

    await _loop(quality=OutputQualityOrchestrator(gate=gate)).run(
        character=_character(), payload=_follow_up_payload(),
        compose=composer.compose,
    )

    context = gate.seen[0]
    assert context.response_text == "那個規則我查完了會跟你說"
    assert context.character_id == "char-1"
    assert context.operator_id == "op-1"
    assert context.operator_primary_language == "zh-TW"
    # The player's newest queued message is what the reply owes an answer to.
    assert context.latest_user_message == "那個規則到底怎麼算"
    material = "\n".join(context.known_material)
    assert "等開完會我再仔細回你" in material
    assert "上週聊過規則書" in material
    # QG4 ships no tool prompts through this seam.
    assert context.tool_prompt_lines == ()


# --------------------------------------------------------------------
# Order: quality first, honesty second, over the same prose
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_honesty_gate_reviews_what_quality_regenerated() -> None:
    """One prose, two gates, in that order.

    The honesty judge must see the draft that would actually ship — if it
    reviewed the pre-regeneration text the verdict would be about a
    message nobody receives.
    """
    composer = _ScriptedComposer([
        _prose("嗨 </schema>"),
        _prose("我等等幫你查，晚點跟你說"),
    ])
    gate = _ScriptedQualityGate([_hard(), _ok()])
    judge = _ScriptedJudge([OutcomeClaimVerdict.ok()])
    guard = OutcomeClaimGuard(judge=judge)

    result = await _loop(
        quality=OutputQualityOrchestrator(gate=gate),
        guard=guard,
        tools=(FakeImageTool(),),
    ).run(
        character=_character(), payload=_payload(), compose=composer.compose,
    )

    assert result.content_text == "我等等幫你查，晚點跟你說"
    assert [text for text, _ in judge.seen] == ["我等等幫你查，晚點跟你說"]


@pytest.mark.asyncio
async def test_quality_skip_never_reaches_the_honesty_gate_or_its_counters(
) -> None:
    """A withheld draft is not a dishonest one.

    The honesty gate's park budget is what cancels a promise outright
    (``park_retries_exhausted``); charging a quality skip against it would
    let a stylistic defect kill the promise entirely.
    """
    composer = _ScriptedComposer([
        _prose("嗨 </schema>"),
        _prose("嗨 </schema> 再一次"),
    ])
    gate = _ScriptedQualityGate([_hard(), _hard()])
    judge = _ScriptedJudge([])
    guard = OutcomeClaimGuard(judge=judge)

    result = await _loop(
        quality=OutputQualityOrchestrator(gate=gate),
        guard=guard,
        tools=(FakeImageTool(),),
    ).run(
        character=_character(), payload=_payload(), compose=composer.compose,
    )

    assert result.content_text == ""
    assert judge.seen == []
    assert guard.counters.parked == 0
    assert guard.counters.blocked_zero_call == 0


@pytest.mark.asyncio
async def test_honesty_recompose_does_not_come_back_through_quality() -> None:
    """R-QG-2, pinned rather than merely written down.

    The honesty correction's product ships on the honesty gate's own
    verdict. Re-reviewing it here would make the two gates able to
    ping-pong a single round between them.
    """
    composer = _ScriptedComposer([
        _prose("畫好囉！圖片附上～"),
        _prose("我等等畫給你看"),
    ])
    gate = _ScriptedQualityGate([_ok()])
    guard = OutcomeClaimGuard(judge=_ScriptedJudge([
        OutcomeClaimVerdict.blocked(("圖片附上",)),
        OutcomeClaimVerdict.ok(),
    ]))

    result = await _loop(
        quality=OutputQualityOrchestrator(gate=gate),
        guard=guard,
        tools=(FakeImageTool(),),
    ).run(
        character=_character(), payload=_payload(), compose=composer.compose,
    )

    assert result.content_text == "我等等畫給你看"
    # Exactly one quality review: the first draft. The honesty retry's
    # prose is not re-reviewed.
    assert len(gate.seen) == 1


# --------------------------------------------------------------------
# The pass-2 exit — where a tool has already spent something
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pass_two_quality_hard_failure_with_nothing_produced_sends_nothing(
) -> None:
    """A round that produced no artifact is cheap to repeat, so it is."""
    composer = _ScriptedComposer([
        _call("echo"),
        _prose("查到了 </schema>"),
        _prose("查到了 </schema> 再一次"),
    ])
    gate = _ScriptedQualityGate([_hard(), _hard()])
    guard = OutcomeClaimGuard(judge=_ScriptedJudge([]))

    result = await _loop(
        quality=OutputQualityOrchestrator(gate=gate),
        guard=guard,
        tools=(EchoTool(),),
    ).run(
        character=_character(allowed=("echo",)),
        payload=_payload(),
        compose=composer.compose,
    )

    assert result.content_text == ""
    assert result.attachments == ()
    assert guard.counters.parked == 0


@pytest.mark.asyncio
async def test_pass_two_quality_hard_failure_still_ships_a_produced_picture(
) -> None:
    """The prose is withheld; the render is not thrown away.

    Discarding a picture the GPU already made would re-render it on every
    reconcile forever — the exact trap ``_no_final_text`` exists to avoid.
    So the fixed localized line stands in for the prose, and the fixed
    line is itself never reviewed (it is a constant).
    """
    composer = _ScriptedComposer([
        _call(),
        _prose("畫好了 </schema>"),
        _prose("畫好了 </schema> 再一次"),
    ])
    gate = _ScriptedQualityGate([_hard(), _hard()])
    guard = OutcomeClaimGuard(judge=_ScriptedJudge([]))

    result = await _loop(
        quality=OutputQualityOrchestrator(gate=gate),
        guard=guard,
        tools=(FakeImageTool(),),
    ).run(
        character=_character(), payload=_payload(), compose=composer.compose,
    )

    assert result.content_text == localized_fallback_text(
        DELIVERED_WITHOUT_TEXT_FALLBACK_KEY, "zh-TW",
    )
    assert len(result.attachments) == 1
    # Two reviews — the draft and its regeneration. The fallback line is
    # never a third.
    assert len(gate.seen) == 2


@pytest.mark.asyncio
async def test_pass_two_quality_pass_leaves_the_delivery_untouched() -> None:
    composer = _ScriptedComposer([_call(), _prose("圖畫好了，附上給你")])
    gate = _ScriptedQualityGate([_ok()])
    guard = OutcomeClaimGuard(judge=_ScriptedJudge([OutcomeClaimVerdict.ok()]))

    result = await _loop(
        quality=OutputQualityOrchestrator(gate=gate),
        guard=guard,
        tools=(FakeImageTool(),),
    ).run(
        character=_character(), payload=_payload(), compose=composer.compose,
    )

    assert result.content_text == "圖畫好了，附上給你"
    assert len(result.attachments) == 1
    assert gate.seen[0].known_material  # the tool output is material
    assert any(
        "已產生" in line for line in gate.seen[0].known_material
    )


@pytest.mark.asyncio
async def test_pass_two_regeneration_keeps_the_tool_results_in_front_of_it(
) -> None:
    """The regenerated draft is written from the same tool outcomes.

    That is what keeps the quality retry from inventing a *different*
    story about what the tools did — and what lets the honesty gate,
    which runs after, judge one consistent round.
    """
    composer = _ScriptedComposer([
        _call(),
        _prose("畫好了 </schema>"),
        _prose("圖畫好了，附上給你"),
    ])
    gate = _ScriptedQualityGate([_hard(), _ok()])
    guard = OutcomeClaimGuard(judge=_ScriptedJudge([OutcomeClaimVerdict.ok()]))

    result = await _loop(
        quality=OutputQualityOrchestrator(gate=gate),
        guard=guard,
        tools=(FakeImageTool(),),
    ).run(
        character=_character(), payload=_payload(), compose=composer.compose,
    )

    assert result.content_text == "圖畫好了，附上給你"
    retry_payload = composer.seen[2]
    assert retry_payload.tool_results
    assert retry_payload.available_tools == ()
    assert "</schema>" not in retry_payload.honesty_correction


# --------------------------------------------------------------------
# The zero-call exit
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_zero_call_quality_regeneration_that_asks_for_a_tool_is_not_prose(
) -> None:
    """A retry that answers with a tool call has written no second draft.

    The quality feedback is about prose; a model that answers it with
    JSON has not produced something to ship, so the hard-fail disposal
    applies and the round retries next tick — nothing was spent.
    """
    composer = _ScriptedComposer([_prose("畫好了 </schema>"), _call()])
    gate = _ScriptedQualityGate([_hard()])
    guard = OutcomeClaimGuard(judge=_ScriptedJudge([]))

    result = await _loop(
        quality=OutputQualityOrchestrator(gate=gate),
        guard=guard,
        tools=(FakeImageTool(),),
    ).run(
        character=_character(), payload=_payload(), compose=composer.compose,
    )

    assert result.content_text == ""
    assert result.attachments == ()


@pytest.mark.asyncio
async def test_empty_pass_one_prose_is_not_reviewed() -> None:
    """Nothing to judge, and the composers' own retry-next-tick already
    covers it — a gate call here would be spent on the empty string."""
    composer = _ScriptedComposer([_prose("")])
    gate = _ScriptedQualityGate([])

    result = await _loop(quality=OutputQualityOrchestrator(gate=gate)).run(
        character=_character(), payload=_payload(), compose=composer.compose,
    )

    assert result.content_text == ""
    assert gate.seen == []


@pytest.mark.asyncio
async def test_pass_counts_on_the_shared_counters() -> None:
    composer = _ScriptedComposer([_prose("剛開完會，我回來了")])
    gate = _ScriptedQualityGate([_ok()])
    quality = OutputQualityOrchestrator(gate=gate)

    await _loop(quality=quality).run(
        character=_character(), payload=_payload(), compose=composer.compose,
    )

    assert quality.counters.total(SURFACE_PROMISE, OUTCOME_PASS) == 1


# --------------------------------------------------------------------
# FC1 — one label per hook point
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_busy_defer_follow_up_counts_under_its_own_surface() -> None:
    """The two kinds of pending follow-up share this loop, not a label.

    The container builds exactly one ``ComposerToolLoop`` and hands it to
    the dispatcher for both the busy-defer follow-up and the scheduled
    promise, so a fixed ``surface="promise"`` recorded every busy-defer
    disposal as a promise. An operator watching the hard-skip rate on the
    promise seam was reading two seams added together, and the busy-defer
    one had no series of its own at all.
    """
    composer = _ScriptedComposer([_prose("開完會了，那個規則我看過了")])
    gate = _ScriptedQualityGate([_ok()])
    quality = OutputQualityOrchestrator(gate=gate)

    await _loop(quality=quality).run(
        character=_character(),
        payload=_follow_up_payload(),
        compose=composer.compose,
    )

    assert quality.counters.total(SURFACE_FOLLOW_UP, OUTCOME_PASS) == 1
    assert quality.counters.total(SURFACE_PROMISE, OUTCOME_PASS) == 0


@pytest.mark.asyncio
async def test_the_two_payload_kinds_land_on_separate_series() -> None:
    """One loop instance, one run each, two labels — the AC in one test."""
    quality = OutputQualityOrchestrator(
        gate=_ScriptedQualityGate([_hard(), _hard(), _ok()]),
    )
    loop = _loop(quality=quality)

    await loop.run(
        character=_character(),
        payload=_follow_up_payload(),
        compose=_ScriptedComposer([
            _prose("開完會了 </schema>"), _prose("開完會了 </schema> 再一次"),
        ]).compose,
    )
    await loop.run(
        character=_character(),
        payload=_payload(),
        compose=_ScriptedComposer([_prose("圖等等就傳給你")]).compose,
    )

    assert quality.counters.snapshot() == {
        (SURFACE_FOLLOW_UP, OUTCOME_HARD_SKIPPED): 1,
        (SURFACE_PROMISE, OUTCOME_PASS): 1,
    }


@pytest.mark.asyncio
async def test_an_unrecognised_payload_keeps_the_configured_surface() -> None:
    """A stand-in payload is not evidence of which seam is running.

    The label is derived from the two shipped compose inputs; anything
    else (a caller this loop has not met, a test fake) falls back to the
    surface the loop was constructed with rather than guessing.
    """

    @dataclass(frozen=True)
    class _Stand:
        available_tools: tuple = ()
        tool_results: tuple = ()
        honesty_correction: str = ""
        operator_primary_language: str = "zh-TW"

    quality = OutputQualityOrchestrator(gate=_ScriptedQualityGate([_ok()]))

    await _loop(quality=quality).run(
        character=_character(),
        payload=_Stand(),
        compose=_ScriptedComposer([_prose("嗨")]).compose,
    )

    assert quality.counters.total(SURFACE_PROMISE, OUTCOME_PASS) == 1
