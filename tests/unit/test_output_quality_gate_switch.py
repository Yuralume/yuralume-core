"""RC — the two surfaces that took the quality band without its switches.

``ComposerToolLoop`` and ``FeedCommentReplyService`` both call
``OutputQualityOrchestrator.review`` with neither ``enabled`` nor
``max_retries``. Two things follow, and both are pinned here:

* ``KOKORO_NOVELTY_GATE_ENABLED=false`` is the batch's rollback switch, and
  it does not build ``None`` — it builds a :class:`NullNoveltyGate`, which
  *passes everything*. So a deployment that switched the gate off still ran
  a review per message and still recorded a ``pass`` on the scrape for it:
  a knob that reads as "immaculate quality" precisely when it is off.
* ``KOKORO_NOVELTY_GATE_MAX_RETRIES`` reached every other surface and not
  these two, so an operator turning the regeneration budget down still paid
  for a regeneration here.

The container half is pinned too, because the defect was never in the
services alone: the settings existed, the wiring simply stopped short.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.application.services.composer_tool_loop import ComposerToolLoop
from kokoro_link.application.services.feed_comment_reply_service import (
    FeedCommentReplyService,
)
from kokoro_link.application.services.feed_comment_service import (
    FeedCommentService,
)
from kokoro_link.application.services.output_quality import (
    OutputQualityCounters,
    OutputQualityOrchestrator,
)
from kokoro_link.bootstrap.container import build_container
from kokoro_link.bootstrap.settings import AppSettings, PromptQualitySettings
from kokoro_link.contracts.feed_comment_reply import (
    FeedCommentReplyComposerPort,
    FeedCommentReplyInput,
    FeedCommentReplyOutput,
)
from kokoro_link.contracts.novelty_gate import (
    NoveltyGateContext,
    NoveltyVerdict,
)
from kokoro_link.contracts.scheduled_promise_composer import (
    ScheduledPromiseComposeInput,
    ScheduledPromiseComposeOutput,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.feed_comment import (
    LOCAL_COMMENTER_ID,
    FeedComment,
)
from kokoro_link.domain.entities.feed_post import FeedPost
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.feed_kind import FeedKind
from kokoro_link.domain.value_objects.feed_source import FeedSource
from kokoro_link.infrastructure.repositories.in_memory_feed_comments import (
    InMemoryFeedCommentRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_feed_posts import (
    InMemoryFeedPostRepository,
)

UTC = timezone.utc


def _now() -> datetime:
    return datetime(2026, 8, 26, 12, 0, tzinfo=UTC)


def _character(*, id_: str = "char-1", feed_daily_limit: int = 3) -> Character:
    return replace(
        Character.create(
            name="Aki", summary="", personality=["溫和"], interests=[],
            speaking_style="平鋪直敘", boundaries=[],
            state=CharacterState(
                emotion="neutral", affection=50, fatigue=20, trust=50,
                energy=70,
            ),
            feed_daily_limit=feed_daily_limit,
        ),
        id=id_, user_id="op-1",
    )


class _SpyGate:
    """Records every context it is shown, and answers from a script."""

    def __init__(self, verdicts: list[NoveltyVerdict]) -> None:
        self._verdicts = list(verdicts)
        self.seen: list[NoveltyGateContext] = []

    async def evaluate(
        self, context: NoveltyGateContext, *, character: Character | None = None,
    ) -> NoveltyVerdict:
        self.seen.append(context)
        if not self._verdicts:
            return NoveltyVerdict(passes=True)
        return self._verdicts.pop(0)


def _orchestrator(gate: _SpyGate) -> OutputQualityOrchestrator:
    return OutputQualityOrchestrator(gate=gate, counters=OutputQualityCounters())


def _hard(feedback: str = "洩漏了結構標記") -> NoveltyVerdict:
    return NoveltyVerdict(passes=False, structural_leak=True, feedback=feedback)


# ----------------------------------------------------------------------
# ComposerToolLoop (surface="promise")
# ----------------------------------------------------------------------


@dataclass
class _ScriptedPromiseComposer:
    script: list[ScheduledPromiseComposeOutput]
    seen: list[ScheduledPromiseComposeInput] = field(default_factory=list)

    async def compose(self, payload):  # noqa: ANN001, ANN201
        self.seen.append(payload)
        if not self.script:  # pragma: no cover - a test asked for too many
            raise AssertionError("composer called more times than scripted")
        return self.script.pop(0)


def _promise_payload() -> ScheduledPromiseComposeInput:
    return ScheduledPromiseComposeInput(
        character=_character(),
        promise_intent="晚點跟對方說查到的規則",
        promise_text="等等幫我查一下",
        scheduled_for=_now(),
        current_activity=None,
        just_finished_activity=None,
        recent_dialogue_summary="剛聊完規則書",
        now=_now(),
    )


@pytest.mark.asyncio
async def test_promise_loop_off_switch_runs_no_review_and_counts_nothing() -> None:
    """The rollback switch must actually roll back.

    With ``NullNoveltyGate`` behind the orchestrator, a review that still
    runs returns ``pass`` for every message — so the scrape shows a
    deployment whose quality gate is off as a deployment with a perfect
    pass rate. ``enabled=False`` is the shared band's own answer to that:
    no judge call, and explicitly no counter.
    """
    composer = _ScriptedPromiseComposer([
        ScheduledPromiseComposeOutput(content_text="我查到了，等等說給你聽"),
    ])
    gate = _SpyGate([])
    quality = _orchestrator(gate)
    loop = ComposerToolLoop(
        output_quality_orchestrator=quality,
        reply_quality_gate_enabled=False,
    )

    result = await loop.run(
        character=_character(), payload=_promise_payload(),
        compose=composer.compose,
    )

    assert result.content_text == "我查到了，等等說給你聽"
    assert gate.seen == []
    assert quality.counters.snapshot() == {}
    assert len(composer.seen) == 1


@pytest.mark.asyncio
async def test_promise_loop_reads_the_retry_budget_from_settings() -> None:
    """``max_retries=0`` is "review only": the disposal table still applies,
    there is simply no second draft to pay for."""
    composer = _ScriptedPromiseComposer([
        ScheduledPromiseComposeOutput(content_text="嗨 </schema>"),
    ])
    gate = _SpyGate([_hard()])
    quality = _orchestrator(gate)
    loop = ComposerToolLoop(
        output_quality_orchestrator=quality,
        reply_quality_gate_max_retries=0,
    )

    result = await loop.run(
        character=_character(), payload=_promise_payload(),
        compose=composer.compose,
    )

    assert result.content_text == ""
    # One compose, one review — the regeneration the default budget would
    # have bought never happens.
    assert len(composer.seen) == 1
    assert len(gate.seen) == 1


@pytest.mark.asyncio
async def test_promise_loop_default_budget_is_unchanged() -> None:
    """Nothing about the shipped behaviour moves when the knobs are absent."""
    composer = _ScriptedPromiseComposer([
        ScheduledPromiseComposeOutput(content_text="嗨 </schema>"),
        ScheduledPromiseComposeOutput(content_text="我回來了，剛開完會"),
    ])
    gate = _SpyGate([_hard(), NoveltyVerdict(passes=True)])
    loop = ComposerToolLoop(output_quality_orchestrator=_orchestrator(gate))

    result = await loop.run(
        character=_character(), payload=_promise_payload(),
        compose=composer.compose,
    )

    assert result.content_text == "我回來了，剛開完會"
    assert len(composer.seen) == 2


# ----------------------------------------------------------------------
# FeedCommentReplyService (surface="feed_comment")
# ----------------------------------------------------------------------


class _ScriptedReplyComposer(FeedCommentReplyComposerPort):
    def __init__(self, outputs: list[FeedCommentReplyOutput]) -> None:
        self._outputs = list(outputs)
        self.inputs: list[FeedCommentReplyInput] = []

    async def compose(
        self, payload: FeedCommentReplyInput,
    ) -> FeedCommentReplyOutput:
        self.inputs.append(payload)
        if not self._outputs:
            return FeedCommentReplyOutput(content_text="")
        return self._outputs.pop(0)


async def _feed_setup(
    *,
    composer: _ScriptedReplyComposer,
    quality: OutputQualityOrchestrator,
    enabled: bool = True,
    max_retries: int = 1,
):
    posts = InMemoryFeedPostRepository()
    comments = InMemoryFeedCommentRepository()
    character = _character(id_="aiko")
    post = FeedPost.create(
        character_id=character.id,
        kind=FeedKind.MOOD,
        content_text="今天的咖啡好喝",
        source=FeedSource.silence(),
        created_at=_now() - timedelta(hours=1),
    )
    await posts.add(post)
    await comments.add(FeedComment.create(
        post_id=post.id,
        author_id=LOCAL_COMMENTER_ID,
        content_text="這杯看起來好好喝",
        created_at=_now() - timedelta(minutes=10),
    ))
    service = FeedCommentReplyService(
        post_repository=posts,
        comment_repository=comments,
        comment_service=FeedCommentService(
            post_repository=posts, comment_repository=comments,
        ),
        composer=composer,
        output_quality_orchestrator=quality,
        reply_quality_gate_enabled=enabled,
        reply_quality_gate_max_retries=max_retries,
    )
    return service, character, comments


@pytest.mark.asyncio
async def test_feed_reply_off_switch_runs_no_review_and_counts_nothing() -> None:
    composer = _ScriptedReplyComposer([
        FeedCommentReplyOutput(content_text="謝啦～改天請你喝 ☕"),
    ])
    gate = _SpyGate([])
    quality = _orchestrator(gate)
    service, character, _comments = await _feed_setup(
        composer=composer, quality=quality, enabled=False,
    )

    reply = await service.tick(character, now=_now())

    assert reply is not None
    assert reply.content_text == "謝啦～改天請你喝 ☕"
    assert gate.seen == []
    assert quality.counters.snapshot() == {}
    assert len(composer.inputs) == 1


@pytest.mark.asyncio
async def test_feed_reply_reads_the_retry_budget_from_settings() -> None:
    composer = _ScriptedReplyComposer([
        FeedCommentReplyOutput(content_text="第一版回覆 </schema>"),
    ])
    gate = _SpyGate([_hard()])
    quality = _orchestrator(gate)
    service, character, comments = await _feed_setup(
        composer=composer, quality=quality, max_retries=0,
    )

    reply = await service.tick(character, now=_now())

    assert reply is None
    assert len(composer.inputs) == 1
    rows = await comments.list_for_post(composer.inputs[0].post.id, limit=10)
    assert all(row.author_id == LOCAL_COMMENTER_ID for row in rows)


@pytest.mark.asyncio
async def test_feed_reply_default_budget_is_unchanged() -> None:
    composer = _ScriptedReplyComposer([
        FeedCommentReplyOutput(content_text="第一版回覆 </schema>"),
        FeedCommentReplyOutput(content_text="謝啦～改天請你喝 ☕"),
    ])
    gate = _SpyGate([_hard(), NoveltyVerdict(passes=True)])
    service, character, _comments = await _feed_setup(
        composer=composer, quality=_orchestrator(gate),
    )

    reply = await service.tick(character, now=_now())

    assert reply is not None
    assert reply.content_text == "謝啦～改天請你喝 ☕"
    assert len(composer.inputs) == 2


# ----------------------------------------------------------------------
# Container wiring
# ----------------------------------------------------------------------


def test_container_threads_the_quality_switches_into_both_surfaces() -> None:
    """The settings existed; only these two seams never read them."""
    container = build_container(AppSettings(
        database_url="",
        prompt_quality=PromptQualitySettings(
            novelty_gate_enabled=False, novelty_gate_max_retries=2,
        ),
    ))

    feed = container.feed_comment_reply_service
    assert feed is not None
    assert feed._quality_gate_enabled is False  # noqa: SLF001
    assert feed._quality_gate_max_retries == 2  # noqa: SLF001

    dispatcher = container.pending_follow_up_dispatcher
    assert dispatcher is not None
    loop = dispatcher._tool_loop  # noqa: SLF001
    assert loop is not None
    assert loop._quality_gate_enabled is False  # noqa: SLF001
    assert loop._quality_gate_max_retries == 2  # noqa: SLF001


def test_container_defaults_leave_both_surfaces_gated() -> None:
    container = build_container(AppSettings(database_url=""))

    feed = container.feed_comment_reply_service
    assert feed is not None
    assert feed._quality_gate_enabled is True  # noqa: SLF001
    assert feed._quality_gate_max_retries == 1  # noqa: SLF001

    loop = container.pending_follow_up_dispatcher._tool_loop  # noqa: SLF001
    assert loop is not None
    assert loop._quality_gate_enabled is True  # noqa: SLF001
    assert loop._quality_gate_max_retries == 1  # noqa: SLF001
