from __future__ import annotations

from datetime import datetime, timezone

import pytest

from kokoro_link.application.services.feed_composer_service import FeedComposerService
from kokoro_link.application.services.proactive_dispatcher import ProactiveDispatcher
from kokoro_link.contracts.feed import FeedComposerInput, FeedComposerOutput
from kokoro_link.contracts.novelty_gate import NoveltyGateContext, NoveltyVerdict
from kokoro_link.contracts.proactive import ProactiveContext, ProactiveDecision
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.feed_kind import FeedKind
from kokoro_link.domain.value_objects.feed_source import FeedSource
from kokoro_link.domain.value_objects.proactive_trigger import ProactiveTrigger


class _Gate:
    def __init__(self, verdict: NoveltyVerdict) -> None:
        self.verdict = verdict
        self.calls: list[NoveltyGateContext] = []

    async def evaluate(self, context: NoveltyGateContext, *, character=None):  # noqa: ANN001
        del character
        self.calls.append(context)
        return self.verdict


class _Decider:
    def __init__(self, retry_message: str) -> None:
        self.retry_message = retry_message
        self.calls: list[ProactiveContext] = []

    async def decide(self, context: ProactiveContext) -> ProactiveDecision:
        self.calls.append(context)
        return ProactiveDecision(
            should_send=True,
            reason="retry",
            message=self.retry_message,
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


@pytest.mark.asyncio
async def test_proactive_reply_quality_gate_retries_decider_once() -> None:
    character = _character()
    dispatcher = ProactiveDispatcher.__new__(ProactiveDispatcher)
    dispatcher._reply_quality_gate_enabled = True  # noqa: SLF001
    dispatcher._reply_quality_gate = _Gate(  # noqa: SLF001
        NoveltyVerdict(passes=False, over_warm=True, feedback="收掉安撫模板"),
    )
    dispatcher._reply_quality_gate_max_retries = 1  # noqa: SLF001
    dispatcher._register_profile_enabled = False  # noqa: SLF001
    dispatcher._register_profiler = None  # noqa: SLF001
    dispatcher._decider = _Decider("retry proactive")  # noqa: SLF001
    context = ProactiveContext(
        character=character,
        trigger=ProactiveTrigger.TICK,
        now=datetime(2026, 6, 22, tzinfo=timezone.utc),
        current_activity=None,
        upcoming_activities=[],
        schedule=None,
        idle_minutes=120.0,
        sent_today=0,
        last_proactive_at=None,
        recent_dialogue_summary="最近只是日常閒聊。",
    )
    decision = ProactiveDecision(
        should_send=True,
        reason="initial",
        message="initial proactive",
    )

    selected, metadata = await dispatcher._gate_proactive_decision(  # noqa: SLF001
        context=context,
        decision=decision,
        character=character,
    )

    assert selected.message == "retry proactive"
    assert dispatcher._reply_quality_gate.calls[0].response_text == "initial proactive"  # noqa: SLF001
    assert dispatcher._decider.calls[0].recent_dialogue_summary.endswith("收掉安撫模板")  # noqa: SLF001
    assert metadata["reply_quality_gate"]["retry_count"] == 1
    assert metadata["reply_quality_gate"]["over_warm"] is True


@pytest.mark.asyncio
async def test_feed_reply_quality_gate_retries_composer_once() -> None:
    character = _character()
    service = FeedComposerService.__new__(FeedComposerService)
    service._reply_quality_gate_enabled = True  # noqa: SLF001
    service._reply_quality_gate = _Gate(  # noqa: SLF001
        NoveltyVerdict(passes=False, formulaic=True, feedback="換一個具體角度"),
    )
    service._reply_quality_gate_max_retries = 1  # noqa: SLF001
    service._register_profile_enabled = False  # noqa: SLF001
    service._register_profiler = None  # noqa: SLF001
    service._composer = _Composer("retry feed post")  # noqa: SLF001
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

    assert output.content_text == "retry feed post"
    assert service._reply_quality_gate.calls[0].response_text == "initial feed post"  # noqa: SLF001
    assert service._composer.calls[0].hint.endswith("換一個具體角度")  # noqa: SLF001
