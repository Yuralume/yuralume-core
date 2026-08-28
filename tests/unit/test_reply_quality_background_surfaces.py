from __future__ import annotations

from datetime import datetime, timezone

import pytest

from kokoro_link.application.services.feed_composer_service import FeedComposerService
from kokoro_link.application.services.output_quality import (
    OutputQualityOrchestrator,
)
from kokoro_link.application.services.output_quality import (
    OUTCOME_HARD_RECOVERED,
    OUTCOME_HARD_SKIPPED,
    OUTCOME_PASS,
    OUTCOME_SOFT_PUBLISHED_BEST_EFFORT,
    OUTCOME_SOFT_RECOVERED,
)
from kokoro_link.application.services.proactive_dispatcher import ProactiveDispatcher
from kokoro_link.contracts.feed import FeedComposerInput, FeedComposerOutput
from kokoro_link.contracts.novelty_gate import NoveltyGateContext, NoveltyVerdict
from kokoro_link.contracts.proactive import ProactiveContext, ProactiveDecision
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.feed_kind import FeedKind
from kokoro_link.domain.value_objects.feed_source import FeedSource
from kokoro_link.domain.value_objects.proactive_trigger import ProactiveTrigger
from kokoro_link.domain.value_objects.tool_call import ToolCall
from kokoro_link.infrastructure.repositories.in_memory_feed_posts import (
    InMemoryFeedPostRepository,
)


class _Gate:
    """Scripted judge: one verdict per call, last one repeats forever."""

    def __init__(self, *verdicts: NoveltyVerdict) -> None:
        self._verdicts = list(verdicts)
        self.calls: list[NoveltyGateContext] = []

    @property
    def verdict(self) -> NoveltyVerdict:
        return self._verdicts[-1]

    async def evaluate(self, context: NoveltyGateContext, *, character=None):  # noqa: ANN001
        del character
        self.calls.append(context)
        index = min(len(self.calls) - 1, len(self._verdicts) - 1)
        return self._verdicts[index]


class _Decider:
    def __init__(
        self,
        retry_message: str,
        *,
        should_send: bool = True,
        retry_tool_calls: tuple[ToolCall, ...] = (),
    ) -> None:
        self.retry_message = retry_message
        self.should_send = should_send
        self.retry_tool_calls = retry_tool_calls
        self.calls: list[ProactiveContext] = []

    async def decide(self, context: ProactiveContext) -> ProactiveDecision:
        self.calls.append(context)
        return ProactiveDecision(
            should_send=self.should_send,
            reason="retry",
            message=self.retry_message if self.should_send else None,
            tool_calls=self.retry_tool_calls,
        )


class _Composer:
    def __init__(self, retry_text: str) -> None:
        self.retry_text = retry_text
        self.calls: list[FeedComposerInput] = []

    async def compose(self, payload: FeedComposerInput) -> FeedComposerOutput:
        self.calls.append(payload)
        return FeedComposerOutput(content_text=self.retry_text, media_kind="none")


def _character() -> Character:
    return Character.create(
        name="Mio",
        summary="溫柔但直接的角色",
        personality=["kind"],
        interests=[],
        speaking_style="short and warm",
        boundaries=[],
        state=CharacterState(
            emotion="neutral",
            affection=50,
            fatigue=10,
            trust=50,
            energy=80,
        ),
    )


def _proactive_context(character: Character, **overrides) -> ProactiveContext:
    payload = {
        "character": character,
        "trigger": ProactiveTrigger.TICK,
        "now": datetime(2026, 6, 22, tzinfo=timezone.utc),
        "current_activity": None,
        "upcoming_activities": [],
        "schedule": None,
        "idle_minutes": 120.0,
        "sent_today": 0,
        "last_proactive_at": None,
        "recent_dialogue_summary": "最近只是日常閒聊。",
    }
    payload.update(overrides)
    return ProactiveContext(**payload)


def _proactive_dispatcher(gate: _Gate, decider: _Decider) -> ProactiveDispatcher:
    dispatcher = ProactiveDispatcher.__new__(ProactiveDispatcher)
    dispatcher._reply_quality_gate_enabled = True  # noqa: SLF001
    dispatcher._reply_quality_gate = gate  # noqa: SLF001
    dispatcher._reply_quality_gate_max_retries = 1  # noqa: SLF001
    dispatcher._register_profile_enabled = False  # noqa: SLF001
    dispatcher._register_profiler = None  # noqa: SLF001
    dispatcher._decider = decider  # noqa: SLF001
    # RA: the proactive surface now runs the shared QG band rather than
    # storing a verdict it never acted on.
    dispatcher._output_quality_orchestrator = OutputQualityOrchestrator(  # noqa: SLF001
        gate=gate,
    )
    return dispatcher


@pytest.mark.asyncio
async def test_proactive_reply_quality_gate_retries_decider_once() -> None:
    character = _character()
    gate = _Gate(
        NoveltyVerdict(passes=False, over_warm=True, feedback="收掉安撫模板"),
        NoveltyVerdict(passes=True),
    )
    dispatcher = _proactive_dispatcher(gate, _Decider("retry proactive"))
    context = _proactive_context(character)
    decision = ProactiveDecision(
        should_send=True,
        reason="initial",
        message="initial proactive",
    )

    resolution = await dispatcher._gate_proactive_decision(  # noqa: SLF001
        context=context,
        decision=decision,
        character=character,
    )

    assert resolution.decision.message == "retry proactive"
    assert gate.calls[0].response_text == "initial proactive"
    # Background policy: the regenerated draft is re-reviewed, so the
    # second call sees the retry rather than "non-empty is good enough".
    assert gate.calls[1].response_text == "retry proactive"
    assert dispatcher._decider.calls[0].recent_dialogue_summary.endswith("收掉安撫模板")  # noqa: SLF001
    quality = resolution.metadata["reply_quality_gate"]
    assert quality["retry_count"] == 1
    # The row still names the axis that forced the retry even though the
    # draft that shipped passed — otherwise a recovered tick is
    # indistinguishable from an untouched one in the audit trail.
    assert quality["over_warm"] is True
    assert quality["outcome"] == OUTCOME_SOFT_RECOVERED
    assert quality["hard_fail"] is False
    # A draft that shipped is not a withhold, whatever it cost to get there.
    assert resolution.withheld is False


@pytest.mark.asyncio
async def test_proactive_gate_context_carries_operator_primary_language() -> None:
    """Without this field the judge's rubric forces ``language_mismatch``
    false, so the 晶晶體 axis could never fire on the highest-volume
    outbound surface."""
    character = _character()
    gate = _Gate(NoveltyVerdict(passes=True))
    dispatcher = _proactive_dispatcher(gate, _Decider("unused"))
    context = _proactive_context(character, operator_primary_language="ja-JP")

    resolution = await dispatcher._gate_proactive_decision(  # noqa: SLF001
        context=context,
        decision=ProactiveDecision(
            should_send=True, reason="initial", message="こんばんは",
        ),
        character=character,
    )

    assert gate.calls[0].operator_primary_language == "ja-JP"


@pytest.mark.asyncio
async def test_proactive_gate_context_carries_the_drafts_tool_prompts() -> None:
    """The judge cannot see ``tool_prompt_defect`` in a column it is not
    shown — and reads the empty column as clause (a), "the prose shows a
    photo and the prompt is missing"."""
    character = _character()
    gate = _Gate(NoveltyVerdict(passes=True))
    dispatcher = _proactive_dispatcher(gate, _Decider("unused"))

    await dispatcher._gate_proactive_decision(  # noqa: SLF001
        context=_proactive_context(character),
        decision=ProactiveDecision(
            should_send=True,
            reason="initial",
            message="剛剛在陽台拍了張照片給你",
            tool_calls=(
                ToolCall(
                    name="generate_image",
                    arguments={
                        "positive": "1girl, balcony, sunset",
                        "aspect": "portrait",
                        # Empty arguments are noise in the column, not
                        # evidence of a defect.
                        "caption": "",
                    },
                ),
            ),
        ),
        character=character,
    )

    assert gate.calls[0].tool_prompt_lines == (
        "generate_image positive: 1girl, balcony, sunset",
        "generate_image aspect: portrait",
    )


@pytest.mark.asyncio
async def test_proactive_re_review_sees_the_regenerated_drafts_own_prompts() -> None:
    """The re-review judges the second draft, so it must be shown the
    second draft's tool calls — not the ones the first draft ordered."""
    character = _character()
    gate = _Gate(
        NoveltyVerdict(passes=False, over_warm=True, feedback="收掉安撫模板"),
        NoveltyVerdict(passes=True),
    )
    decider = _Decider(
        "換張照片",
        retry_tool_calls=(
            ToolCall(name="generate_image", arguments={"positive": "cat, rain"}),
        ),
    )
    dispatcher = _proactive_dispatcher(gate, decider)

    await dispatcher._gate_proactive_decision(  # noqa: SLF001
        context=_proactive_context(character),
        decision=ProactiveDecision(
            should_send=True,
            reason="initial",
            message="拍了張照片給你",
            tool_calls=(
                ToolCall(name="generate_image", arguments={"positive": "sunset"}),
            ),
        ),
        character=character,
    )

    assert gate.calls[0].tool_prompt_lines == ("generate_image positive: sunset",)
    assert gate.calls[1].tool_prompt_lines == ("generate_image positive: cat, rain",)


@pytest.mark.asyncio
async def test_an_image_push_is_not_hard_skipped_for_a_column_it_never_saw() -> None:
    """The 2026-08-26 defect, reproduced through a judge that applies the
    rubric it is actually given.

    ``gate.txt`` clause (a) fires when the prose shows a picture and the
    工具 prompt column is empty. With the column never populated, that was
    true of *every* push carrying a ``generate_image`` call — a hard axis,
    so background policy withheld each one and the tick ended silently.
    """

    class _RubricGate(_Gate):
        async def evaluate(self, context: NoveltyGateContext, *, character=None):  # noqa: ANN001
            self.calls.append(context)
            claims_photo = "照片" in (context.response_text or "")
            if claims_photo and not context.tool_prompt_lines:
                return NoveltyVerdict(
                    passes=False,
                    tool_prompt_defect=True,
                    feedback="正文在展示一張圖，工具 prompt 卻空缺",
                )
            return NoveltyVerdict(passes=True)

    character = _character()
    gate = _RubricGate()
    decider = _Decider("unused")
    dispatcher = _proactive_dispatcher(gate, decider)
    decision = ProactiveDecision(
        should_send=True,
        reason="initial",
        message="剛剛拍了張照片給你",
        tool_calls=(
            ToolCall(name="generate_image", arguments={"positive": "1girl, cafe"}),
        ),
    )

    resolution = await dispatcher._gate_proactive_decision(  # noqa: SLF001
        context=_proactive_context(character),
        decision=decision,
        character=character,
    )

    assert resolution.decision is decision
    assert resolution.withheld is False
    assert resolution.metadata["reply_quality_gate"]["outcome"] == OUTCOME_PASS
    # Nothing to regenerate, so the decider was never woken.
    assert decider.calls == []


@pytest.mark.asyncio
async def test_proactive_hard_failure_surviving_regeneration_skips_the_tick() -> None:
    character = _character()
    gate = _Gate(
        NoveltyVerdict(
            passes=False, structural_leak=True, feedback="正文混進了 schema 欄位名",
        ),
    )
    decider = _Decider("retry proactive")
    dispatcher = _proactive_dispatcher(gate, decider)
    decision = ProactiveDecision(
        should_send=True,
        reason="initial",
        message='initial proactive {"should_send": true}',
    )

    resolution = await dispatcher._gate_proactive_decision(  # noqa: SLF001
        context=_proactive_context(character),
        decision=decision,
        character=character,
    )

    # Nothing ships this tick, and it leaves through the existing
    # ``not should_send`` path so the attempt is logged as a skip and
    # never as a delivery.
    assert resolution.decision.should_send is False
    assert resolution.decision.message is None
    # The bit the caller needs to log this as ``QUALITY_WITHHELD`` rather
    # than as the character's own choice to stay quiet.
    assert resolution.withheld is True
    assert len(gate.calls) == 2
    assert len(decider.calls) == 1
    quality = resolution.metadata["reply_quality_gate"]
    assert quality["outcome"] == OUTCOME_HARD_SKIPPED
    assert quality["hard_fail"] is True
    assert quality["structural_leak"] is True
    assert quality["retry_count"] == 1
    counters = dispatcher._output_quality_orchestrator.counters  # noqa: SLF001
    assert counters.total("proactive", OUTCOME_HARD_SKIPPED) == 1


@pytest.mark.asyncio
async def test_proactive_hard_failure_regenerated_clean_is_recovered() -> None:
    character = _character()
    gate = _Gate(
        NoveltyVerdict(
            passes=False, language_mismatch=True, feedback="不要夾雜英文",
        ),
        NoveltyVerdict(passes=True),
    )
    dispatcher = _proactive_dispatcher(gate, _Decider("乾淨的重寫"))

    resolution = await dispatcher._gate_proactive_decision(  # noqa: SLF001
        context=_proactive_context(character),
        decision=ProactiveDecision(
            should_send=True, reason="initial", message="今天的 schedule 有點 tight",
        ),
        character=character,
    )

    assert resolution.decision.should_send is True
    assert resolution.decision.message == "乾淨的重寫"
    assert resolution.metadata["reply_quality_gate"]["outcome"] == OUTCOME_HARD_RECOVERED


@pytest.mark.asyncio
async def test_proactive_hard_failure_with_silent_redecide_skips_the_tick() -> None:
    """A re-decide that chose silence is *not* a usable second draft: the
    original defect must not ship in its place."""
    character = _character()
    gate = _Gate(
        NoveltyVerdict(passes=False, visible_truncation=True, feedback="句子斷掉了"),
    )
    dispatcher = _proactive_dispatcher(
        gate, _Decider("", should_send=False),
    )

    resolution = await dispatcher._gate_proactive_decision(  # noqa: SLF001
        context=_proactive_context(character),
        decision=ProactiveDecision(
            should_send=True, reason="initial", message="我剛剛在想，",
        ),
        character=character,
    )

    assert resolution.decision.should_send is False
    # Only the first draft was ever judged — there was no second one.
    assert len(gate.calls) == 1
    assert resolution.metadata["reply_quality_gate"]["outcome"] == OUTCOME_HARD_SKIPPED


@pytest.mark.asyncio
async def test_proactive_soft_failure_ships_best_effort() -> None:
    """The five soft axes are quality opinions; withholding a whole push
    over one costs more than the push is worth."""
    character = _character()
    gate = _Gate(
        NoveltyVerdict(passes=False, formulaic=True, feedback="換個角度"),
    )
    dispatcher = _proactive_dispatcher(gate, _Decider("retry proactive"))

    resolution = await dispatcher._gate_proactive_decision(  # noqa: SLF001
        context=_proactive_context(character),
        decision=ProactiveDecision(
            should_send=True, reason="initial", message="initial proactive",
        ),
        character=character,
    )

    assert resolution.decision.should_send is True
    assert resolution.decision.message == "retry proactive"
    quality = resolution.metadata["reply_quality_gate"]
    assert quality["outcome"] == OUTCOME_SOFT_PUBLISHED_BEST_EFFORT
    assert quality["hard_fail"] is False


@pytest.mark.asyncio
async def test_proactive_quality_gate_noop_without_orchestrator() -> None:
    character = _character()
    gate = _Gate(NoveltyVerdict(passes=False, over_warm=True, feedback="x"))
    dispatcher = _proactive_dispatcher(gate, _Decider("retry proactive"))
    dispatcher._output_quality_orchestrator = None  # noqa: SLF001
    decision = ProactiveDecision(
        should_send=True, reason="initial", message="initial proactive",
    )

    resolution = await dispatcher._gate_proactive_decision(  # noqa: SLF001
        context=_proactive_context(character),
        decision=decision,
        character=character,
    )

    assert resolution.decision is decision
    assert resolution.metadata == {}
    assert gate.calls == []


@pytest.mark.asyncio
async def test_feed_reply_quality_gate_retries_composer_once() -> None:
    character = _character()
    gate = _Gate(
        NoveltyVerdict(passes=False, formulaic=True, feedback="換一個具體角度"),
    )
    service = FeedComposerService.__new__(FeedComposerService)
    service._reply_quality_gate_enabled = True  # noqa: SLF001
    service._reply_quality_gate = gate  # noqa: SLF001
    service._reply_quality_gate_max_retries = 1  # noqa: SLF001
    service._register_profile_enabled = False  # noqa: SLF001
    service._register_profiler = None  # noqa: SLF001
    service._composer = _Composer("retry feed post")  # noqa: SLF001
    # QG2: the judging itself moved into the shared orchestrator, which the
    # container builds around this very gate.
    service._repo = InMemoryFeedPostRepository()  # noqa: SLF001
    service._output_quality_orchestrator = OutputQualityOrchestrator(  # noqa: SLF001
        gate=gate,
    )
    payload = FeedComposerInput(
        character=character,
        kind=FeedKind.DAILY,
        source=FeedSource.silence(),
        hint="寫一則日常貼文",
        context_snippets=("今天在家整理桌面。",),
        image_required=False,
    )

    output = await service._gate_feed_output(  # noqa: SLF001
        composer_input=payload,
        output=FeedComposerOutput(content_text="initial feed post", media_kind="none"),
        operator=None,
    )

    assert output is not None
    assert output.content_text == "retry feed post"
    assert gate.calls[0].response_text == "initial feed post"
    assert service._composer.calls[0].hint.endswith("換一個具體角度")  # noqa: SLF001
