"""QG2 — the feed wall's output-quality band, incident first.

The 2026-08-26 post is the whole reason this file exists, so it is the
first test in it. The model appended its image-prompt tag string to the
caption: a value that is a string, non-empty, and therefore invisible to
every check the composer could make. The parser sliced it to 280
characters (caption ends mid-tag), the picture never rendered because
``image_prompt`` came back empty, and the old gate — five soft axes, one
regeneration, "non-empty means good" — published it.

Four things had to change for that post not to ship, and each has a test
here: the body reaches the judge whole, the judge sees the tool prompts
and the length evidence, a hard failure that survives regeneration
publishes **nothing**, and the one hard failure a post can survive (a
broken tool prompt over clean prose) costs it its picture rather than its
existence.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone, tzinfo
from typing import Any

import pytest

from kokoro_link.application.services.feed_candidates import FeedCandidate
from kokoro_link.application.services.feed_composer_service import (
    FeedComposerService,
)
from kokoro_link.application.services.output_quality import (
    OUTCOME_HARD_DEGRADED,
    OUTCOME_HARD_RECOVERED,
    OUTCOME_HARD_SKIPPED,
    OUTCOME_PASS,
    OutputQualityCounters,
    OutputQualityOrchestrator,
)
from kokoro_link.contracts.feed import (
    FeedComposerInput,
    FeedComposerOutput,
    FeedComposerPort,
)
from kokoro_link.contracts.novelty_gate import NoveltyGateContext, NoveltyVerdict
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.feed_post import FeedPost
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.feed_kind import FeedKind
from kokoro_link.domain.value_objects.feed_source import FeedSource
from kokoro_link.infrastructure.feed.llm_composer import MAX_BODY_CHARS
from kokoro_link.infrastructure.repositories.in_memory_feed_posts import (
    InMemoryFeedPostRepository,
)
from kokoro_link.infrastructure.storage.in_memory import InMemoryObjectStorage

pytestmark = pytest.mark.asyncio

_TICK_AT = datetime(2026, 8, 26, 10, 0, tzinfo=timezone.utc)

_PASS = NoveltyVerdict(passes=True)
_LEAK = NoveltyVerdict(
    passes=False, structural_leak=True, feedback="正文混入 image prompt tag 串",
)
_TOOL_DEFECT = NoveltyVerdict(
    passes=False, tool_prompt_defect=True, feedback="image_prompt 是空的",
)


# ---------- doubles ----------


class _ScriptedGate:
    """Answers with a scripted verdict list; records every context seen."""

    def __init__(self, verdicts: list[NoveltyVerdict]) -> None:
        self._verdicts = list(verdicts)
        self.seen: list[NoveltyGateContext] = []

    async def evaluate(self, context: NoveltyGateContext, *, character=None):  # noqa: ANN001
        del character
        self.seen.append(context)
        if not self._verdicts:
            return _PASS
        return self._verdicts.pop(0)


class _ScriptedComposer(FeedComposerPort):
    def __init__(self, outputs: list[FeedComposerOutput]) -> None:
        self._outputs = list(outputs)
        self.inputs: list[FeedComposerInput] = []

    async def compose(self, payload: FeedComposerInput) -> FeedComposerOutput:
        self.inputs.append(payload)
        if not self._outputs:
            return FeedComposerOutput(content_text="")
        return self._outputs.pop(0)


class _FixedCollector:
    def __init__(self, candidates: list[FeedCandidate]) -> None:
        self._candidates = candidates

    async def collect(
        self, character: Character, *, now: datetime, local_tz: tzinfo = timezone.utc,
    ):
        del character, now, local_tz
        return tuple(self._candidates)


class _RecordingImageProvider:
    provider_id = "stub-image"

    def __init__(self) -> None:
        self.calls = 0

    async def generate(self, **kwargs: Any) -> list[bytes]:
        del kwargs
        self.calls += 1
        return [b"\x89PNG fake"]


class _ExplodingRepository(InMemoryFeedPostRepository):
    """History lookups fail (and are counted); everything else works."""

    def __init__(self) -> None:
        super().__init__()
        self.history_calls = 0

    async def list_for_character(self, character_id: str, **kwargs: Any):
        self.history_calls += 1
        raise RuntimeError("replica unreachable")


# ---------- fixtures-by-hand ----------


def _character() -> Character:
    return Character.create(
        name="Aiko",
        summary="",
        personality=["kind"],
        interests=[],
        speaking_style="短句",
        boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=20, trust=50, energy=80,
        ),
        feed_daily_limit=3,
    )


def _candidate(*, image_required: bool = False) -> FeedCandidate:
    return FeedCandidate(
        kind=FeedKind.MOOD,
        source=FeedSource.silence(),
        hint="寫一則日常貼文",
        score=0.5,
        context_snippets=("今天在家整理桌面。",),
        image_required=image_required,
    )


def _service(
    *,
    gate: _ScriptedGate,
    composer: _ScriptedComposer,
    repository: InMemoryFeedPostRepository | None = None,
    counters: OutputQualityCounters | None = None,
    image_provider: Any = None,
    image_required: bool = False,
    max_retries: int = 1,
) -> tuple[FeedComposerService, InMemoryFeedPostRepository]:
    from tests.unit._image_provider_stub import StaticActiveImageProvider

    repo = repository if repository is not None else InMemoryFeedPostRepository()
    service = FeedComposerService(
        repository=repo,
        candidates=_FixedCollector([_candidate(image_required=image_required)]),
        composer=composer,
        image_provider=(
            StaticActiveImageProvider(image_provider)
            if image_provider is not None else None
        ),
        object_storage=InMemoryObjectStorage(public_base_url="/uploads"),
        reply_quality_gate=gate,
        reply_quality_gate_enabled=True,
        reply_quality_gate_max_retries=max_retries,
        output_quality_orchestrator=OutputQualityOrchestrator(
            gate=gate, counters=counters or OutputQualityCounters(),
        ),
    )
    return service, repo


# ---------- the incident ----------


async def test_a_hard_failure_that_survives_regeneration_publishes_nothing() -> None:
    """The incident, replayed. Reviewed, regenerated, reviewed again, still
    leaking → this tick writes no row and returns no post."""
    counters = OutputQualityCounters()
    composer = _ScriptedComposer([
        FeedComposerOutput(content_text="正文 image_prompt: 1girl, solo, cafe"),
        FeedComposerOutput(content_text="重生稿 image_prompt: 1girl, solo"),
    ])
    gate = _ScriptedGate([_LEAK, _LEAK])
    service, repo = _service(gate=gate, composer=composer, counters=counters)

    post = await service.tick(_character(), now=_TICK_AT)

    assert post is None
    assert await repo.list_recent() == []
    # Regenerated once, and the second draft was actually re-reviewed —
    # the step the pre-QG shape skipped.
    assert len(composer.inputs) == 2
    assert [ctx.response_text for ctx in gate.seen] == [
        "正文 image_prompt: 1girl, solo, cafe",
        "重生稿 image_prompt: 1girl, solo",
    ]
    assert counters.total("feed", OUTCOME_HARD_SKIPPED) == 1


async def test_the_regeneration_carries_the_judges_feedback() -> None:
    composer = _ScriptedComposer([
        FeedComposerOutput(content_text="第一稿"),
        FeedComposerOutput(content_text="乾淨的第二稿"),
    ])
    gate = _ScriptedGate([_LEAK, _PASS])
    counters = OutputQualityCounters()
    service, repo = _service(gate=gate, composer=composer, counters=counters)

    post = await service.tick(_character(), now=_TICK_AT)

    assert post is not None
    assert post.content_text == "乾淨的第二稿"
    assert composer.inputs[1].hint.endswith("正文混入 image prompt tag 串")
    assert counters.total("feed", OUTCOME_HARD_RECOVERED) == 1


async def test_a_skipped_tick_hands_its_quota_slot_back() -> None:
    """The skip reuses the existing "candidate produced nothing" path, so a
    withheld post must not also cost the character its daily slot."""
    discarded: list[str | None] = []
    composer = _ScriptedComposer([
        FeedComposerOutput(content_text="第一稿"),
        FeedComposerOutput(content_text="第二稿"),
    ])
    service, _ = _service(gate=_ScriptedGate([_LEAK, _LEAK]), composer=composer)

    async def _record(claim_id: str | None) -> None:
        discarded.append(claim_id)

    service._discard_runtime_feed_post_claim = _record  # noqa: SLF001

    assert await service.tick(_character(), now=_TICK_AT) is None
    assert len(discarded) == 1


# ---------- RD: a hard failure stops the whole tick ----------


async def test_a_hard_skip_ends_the_tick_without_trying_the_next_candidate() -> None:
    """The old shape read a hard-failed candidate's ``_TickOutcome()`` as
    "composer no-op" and fell through to candidate #2 — a second compose
    and a second judge call in the same tick, and #2 could still publish.
    D1's "skip this tick" meant the whole tick, not just the candidate
    that failed."""
    counters = OutputQualityCounters()
    composer = _ScriptedComposer([
        FeedComposerOutput(content_text="正文 image_prompt: 1girl, solo, cafe"),
        FeedComposerOutput(content_text="重生稿 image_prompt: 1girl, solo"),
        FeedComposerOutput(content_text="候選二的稿子，不該被叫到"),
    ])
    gate = _ScriptedGate([_LEAK, _LEAK])
    repo = InMemoryFeedPostRepository()
    candidate_one = _candidate()
    candidate_two = replace(
        candidate_one, source=FeedSource(kind="manual", ref_id="candidate-two"),
    )
    service = FeedComposerService(
        repository=repo,
        candidates=_FixedCollector([candidate_one, candidate_two]),
        composer=composer,
        object_storage=InMemoryObjectStorage(public_base_url="/uploads"),
        reply_quality_gate=gate,
        reply_quality_gate_enabled=True,
        reply_quality_gate_max_retries=1,
        output_quality_orchestrator=OutputQualityOrchestrator(
            gate=gate, counters=counters,
        ),
    )

    post = await service.tick(_character(), now=_TICK_AT)

    assert post is None
    assert await repo.list_recent() == []
    # Candidate #1 was composed twice (initial + regeneration). Candidate
    # #2's compose was never reached at all.
    assert len(composer.inputs) == 2
    assert counters.total("feed", OUTCOME_HARD_SKIPPED) == 1


async def test_a_plain_composer_no_op_still_falls_through_to_the_next_candidate() -> None:
    """Regression lock: only a *hard* gate failure ends the tick. A
    candidate the composer itself declines (empty body) must still hand
    the slot to the next candidate, exactly as before this batch."""
    composer = _ScriptedComposer([
        FeedComposerOutput(content_text=""),
        FeedComposerOutput(content_text="候選二順利發出。"),
    ])
    gate = _ScriptedGate([_PASS])
    repo = InMemoryFeedPostRepository()
    candidate_one = _candidate()
    candidate_two = replace(
        candidate_one, source=FeedSource(kind="manual", ref_id="candidate-two"),
    )
    service = FeedComposerService(
        repository=repo,
        candidates=_FixedCollector([candidate_one, candidate_two]),
        composer=composer,
        object_storage=InMemoryObjectStorage(public_base_url="/uploads"),
        reply_quality_gate=gate,
        reply_quality_gate_enabled=True,
        reply_quality_gate_max_retries=1,
        output_quality_orchestrator=OutputQualityOrchestrator(
            gate=gate, counters=OutputQualityCounters(),
        ),
    )

    post = await service.tick(_character(), now=_TICK_AT)

    assert post is not None
    assert post.content_text == "候選二順利發出。"
    assert len(composer.inputs) == 2


# ---------- the degrade exception ----------


async def test_an_unfixable_tool_prompt_degrades_to_a_text_only_post() -> None:
    """Prose is clean, the picture is not: publish the post without it.

    Recorded as ``hard_degraded`` rather than ``hard_skipped`` on purpose —
    the skip counter is an alert line, and a post that merely lost its
    picture must not ring it.
    """
    counters = OutputQualityCounters()
    provider = _RecordingImageProvider()
    composer = _ScriptedComposer([
        FeedComposerOutput(content_text="今天的雲很好看。", image_prompt="1girl"),
        FeedComposerOutput(content_text="今天的雲很好看。", image_prompt="1girl"),
    ])
    service, repo = _service(
        gate=_ScriptedGate([_TOOL_DEFECT, _TOOL_DEFECT]),
        composer=composer,
        counters=counters,
        image_provider=provider,
        image_required=True,
    )

    post = await service.tick(_character(), now=_TICK_AT)

    assert post is not None
    assert post.content_text == "今天的雲很好看。"
    assert post.image_url is None
    assert post.image_prompt is None
    assert provider.calls == 0  # nothing was rendered from the broken prompt
    assert len(await repo.list_recent()) == 1
    assert counters.total("feed", OUTCOME_HARD_DEGRADED) == 1
    assert counters.total("feed", OUTCOME_HARD_SKIPPED) == 0


async def test_a_leak_alongside_a_broken_prompt_is_still_withheld() -> None:
    """The degrade is not a general softening: it applies only when the
    prose itself is clean."""
    both = NoveltyVerdict(
        passes=False, tool_prompt_defect=True, structural_leak=True,
    )
    composer = _ScriptedComposer([
        FeedComposerOutput(content_text="正文 {\"image_prompt\"", image_prompt=""),
        FeedComposerOutput(content_text="正文 {\"image_prompt\"", image_prompt=""),
    ])
    service, repo = _service(
        gate=_ScriptedGate([both, both]),
        composer=composer,
        image_required=True,
    )

    assert await service.tick(_character(), now=_TICK_AT) is None
    assert await repo.list_recent() == []


# ---------- what the judge is shown ----------


async def test_an_overlong_body_reaches_the_judge_whole_and_is_capped_after() -> None:
    body = "字" * 400
    gate = _ScriptedGate([_PASS])
    composer = _ScriptedComposer([FeedComposerOutput(content_text=body)])
    service, _ = _service(gate=gate, composer=composer)

    post = await service.tick(_character(), now=_TICK_AT)

    # The judge saw all 400 characters, plus the deterministic evidence
    # line saying so — that is what buys the model a chance to rewrite.
    assert gate.seen[0].response_text == body
    evidence = " ".join(gate.seen[0].mechanical_evidence_lines)
    assert "400" in evidence
    assert str(MAX_BODY_CHARS) in evidence
    # Only then, on a draft the gate accepted, does the cap apply.
    assert post is not None
    assert post.content_text == "字" * MAX_BODY_CHARS


async def test_a_body_inside_the_cap_carries_no_overrun_evidence() -> None:
    gate = _ScriptedGate([_PASS])
    composer = _ScriptedComposer([FeedComposerOutput(content_text="短短一句。")])
    service, _ = _service(gate=gate, composer=composer)

    await service.tick(_character(), now=_TICK_AT)

    assert gate.seen[0].mechanical_evidence_lines == ()


async def test_the_tool_prompts_are_shown_labelled_and_outside_the_prose() -> None:
    """Both halves of ``tool_prompt_lines``: the judge cannot see
    ``tool_prompt_defect`` without them, and must not read their English
    tags as a language mismatch in a Chinese post."""
    gate = _ScriptedGate([_PASS])
    composer = _ScriptedComposer([
        FeedComposerOutput(
            content_text="今天的雲很好看。",
            image_prompt="1girl, solo, cafe",
            video_prompt="Anime style, slow dolly.",
            media_kind="video",
        ),
    ])
    service, _ = _service(gate=gate, composer=composer, image_required=True)

    await service.tick(_character(), now=_TICK_AT)

    assert gate.seen[0].tool_prompt_lines == (
        "image_prompt: 1girl, solo, cafe",
        "video_prompt: Anime style, slow dolly.",
    )
    assert "1girl" not in gate.seen[0].response_text


async def test_an_empty_tool_prompt_contributes_no_line() -> None:
    gate = _ScriptedGate([_PASS])
    composer = _ScriptedComposer([
        FeedComposerOutput(content_text="今天的雲很好看。", image_prompt="  "),
    ])
    service, _ = _service(gate=gate, composer=composer, image_required=True)

    await service.tick(_character(), now=_TICK_AT)

    assert gate.seen[0].tool_prompt_lines == ()


async def test_the_operator_language_reaches_the_judge() -> None:
    gate = _ScriptedGate([_PASS])
    composer = _ScriptedComposer([FeedComposerOutput(content_text="短短一句。")])
    service, _ = _service(gate=gate, composer=composer)

    await service.tick(_character(), now=_TICK_AT)

    assert gate.seen[0].operator_primary_language == "zh-TW"


# ---------- real diversity evidence ----------


async def test_recent_posts_are_the_self_repetition_material() -> None:
    """Before QG2 this was a hard-coded ``assistant_line_count=0`` and one
    generic sentence — a self-repetition check with nothing to compare
    against, which is to say no check at all."""
    repo = InMemoryFeedPostRepository()
    for index in range(6):
        await repo.add(
            FeedPost.create(
                character_id="c-diverse",
                kind=FeedKind.MOOD,
                content_text=f"第 {index} 篇舊貼文",
                source=FeedSource(kind="manual", ref_id=f"seed-{index}"),
                created_at=datetime(2026, 8, 20 + index, 9, 0, tzinfo=timezone.utc),
            ),
        )
    gate = _ScriptedGate([_PASS])
    composer = _ScriptedComposer([FeedComposerOutput(content_text="新的一句。")])
    service, _ = _service(gate=gate, composer=composer, repository=repo)

    await service.tick(
        replace(_character(), id="c-diverse", feed_daily_limit=99),
        now=datetime(2026, 8, 27, 10, 0, tzinfo=timezone.utc),
    )

    context = gate.seen[0]
    assert len(context.recent_self_lines) == 5  # newest window, not all six
    assert "第 5 篇舊貼文" in context.recent_self_lines
    assert context.diversity_evidence is not None
    assert context.diversity_evidence.assistant_line_count == 5


async def test_a_failed_history_lookup_degrades_to_an_empty_window() -> None:
    """Fail-soft: a broken read costs the novelty axes their material, not
    the post."""
    gate = _ScriptedGate([_PASS])
    composer = _ScriptedComposer([FeedComposerOutput(content_text="新的一句。")])
    service, _ = _service(
        gate=gate, composer=composer, repository=_ExplodingRepository(),
    )

    post = await service.tick(_character(), now=_TICK_AT)

    assert post is not None
    assert gate.seen[0].recent_self_lines == ()
    assert gate.seen[0].diversity_evidence.assistant_line_count == 0
    assert gate.seen[0].diversity_evidence.phrase_frequency_lines != ()


# ---------- wiring off ----------


async def test_an_unwired_orchestrator_publishes_exactly_as_before() -> None:
    """Self-host and every legacy caller: no orchestrator, no review, no
    behaviour change."""
    repo = InMemoryFeedPostRepository()
    composer = _ScriptedComposer([FeedComposerOutput(content_text="就這樣發出去。")])
    service = FeedComposerService(
        repository=repo,
        candidates=_FixedCollector([_candidate()]),
        composer=composer,
    )

    post = await service.tick(_character(), now=_TICK_AT)

    assert post is not None
    assert post.content_text == "就這樣發出去。"
    assert len(composer.inputs) == 1


async def test_the_disabled_flag_skips_the_judge_without_counting() -> None:
    """And without paying for the evidence either: the history query and
    the register profile are both on the far side of this check."""
    counters = OutputQualityCounters()
    gate = _ScriptedGate([_LEAK, _LEAK])
    composer = _ScriptedComposer([FeedComposerOutput(content_text="就這樣發出去。")])
    repo = _ExplodingRepository()
    service = FeedComposerService(
        repository=repo,
        candidates=_FixedCollector([_candidate()]),
        composer=composer,
        reply_quality_gate=gate,
        reply_quality_gate_enabled=False,
        output_quality_orchestrator=OutputQualityOrchestrator(
            gate=gate, counters=counters,
        ),
    )

    post = await service.tick(_character(), now=_TICK_AT)

    assert post is not None
    assert gate.seen == []
    assert counters.snapshot() == {}
    assert repo.history_calls == 0


async def test_a_clean_draft_is_published_untouched() -> None:
    counters = OutputQualityCounters()
    composer = _ScriptedComposer([
        FeedComposerOutput(content_text="今天的雲很好看。"),
    ])
    service, _ = _service(
        gate=_ScriptedGate([_PASS]), composer=composer, counters=counters,
    )

    post = await service.tick(_character(), now=_TICK_AT)

    assert post is not None
    assert post.content_text == "今天的雲很好看。"
    assert len(composer.inputs) == 1
    assert counters.total("feed", OUTCOME_PASS) == 1
