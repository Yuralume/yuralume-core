"""QG0 — the shared disposal band, both policies, every exit.

The matrix below is the D1 table turned into assertions. It is worth
spelling out in full because the two policies are asymmetric in a way that
is easy to "simplify" back into a bug: the background side must be willing
to send **nothing**, and the chat side must never be. A refactor that
unifies them collapses either 寧靜勿爛 (background ships a defect) or the
typing indicator (chat hangs), and only a test that names both catches it.
"""

from __future__ import annotations

import pytest

from kokoro_link.application.services.output_quality import (
    OUTCOME_GATE_ERROR_FAILOPEN,
    OUTCOME_HARD_DEGRADED,
    OUTCOME_HARD_PUBLISHED_BEST_EFFORT,
    OUTCOME_HARD_RECOVERED,
    OUTCOME_HARD_SKIPPED,
    OUTCOME_PASS,
    OUTCOME_SOFT_PUBLISHED_BEST_EFFORT,
    OUTCOME_SOFT_RECOVERED,
    OutputQualityCounters,
    OutputQualityOrchestrator,
    OutputQualityPolicy,
    fired_axes,
)
from kokoro_link.contracts.novelty_gate import NoveltyGateContext, NoveltyVerdict

pytestmark = pytest.mark.asyncio

BACKGROUND = OutputQualityPolicy.BACKGROUND_FAIL_CLOSED
CHAT = OutputQualityPolicy.CHAT_BEST_EFFORT

_PASS = NoveltyVerdict(passes=True)
_SOFT = NoveltyVerdict(passes=False, over_warm=True, feedback="太黏了")
_HARD = NoveltyVerdict(
    passes=False, structural_leak=True, feedback="正文混入 schema 片段",
)
_TOOL = NoveltyVerdict(
    passes=False, tool_prompt_defect=True, feedback="image_prompt 是空的",
)
_TOOL_AND_LEAK = NoveltyVerdict(
    passes=False, tool_prompt_defect=True, structural_leak=True,
)
_DEGRADABLE = frozenset({"tool_prompt_defect"})


class _Gate:
    """Answers with a scripted verdict list; records every candidate seen.

    ``raises`` makes the *next* call blow up, which is how a judge that is
    actually down behaves — the port promises never to raise and this is
    the test that the orchestrator does not believe it.
    """

    def __init__(self, verdicts, *, raises: bool = False) -> None:
        self._verdicts = list(verdicts)
        self._raises = raises
        self.seen: list[str] = []

    async def evaluate(self, context: NoveltyGateContext, *, character=None):
        self.seen.append(context.response_text)
        if self._raises:
            raise RuntimeError("judge down")
        if not self._verdicts:
            return _PASS
        return self._verdicts.pop(0)


def _context(candidate: str) -> NoveltyGateContext:
    return NoveltyGateContext(
        character_id="char-1", operator_id="op-1", response_text=candidate,
    )


def _orchestrator(gate, counters: OutputQualityCounters | None = None):
    return OutputQualityOrchestrator(
        gate=gate, counters=counters or OutputQualityCounters(),
    )


async def _review(gate, candidate="原稿", *, policy, retry="重生稿", **kwargs):
    calls: list[str] = []

    async def regenerate(feedback: str):
        calls.append(feedback)
        return retry

    orchestrator = kwargs.pop("orchestrator", None) or _orchestrator(gate)
    review = await orchestrator.review(
        candidate,
        surface="feed",
        context_for=_context,
        regenerate=None if retry is _NO_REGEN else regenerate,
        policy=policy,
        **kwargs,
    )
    return review, calls


_NO_REGEN = object()


# ── pass and gate error, identical on both policies ──────────────────


@pytest.mark.parametrize("policy", [BACKGROUND, CHAT])
async def test_pass_ships_the_original_untouched(policy) -> None:
    gate = _Gate([_PASS])

    review, calls = await _review(gate, policy=policy)

    assert review.outcome == OUTCOME_PASS
    assert review.final == "原稿"
    assert review.regen_attempted is False
    assert calls == []


@pytest.mark.parametrize("policy", [BACKGROUND, CHAT])
async def test_unrouted_first_review_passes_without_counting(policy) -> None:
    """``pass_unrouted`` is "no judge is routable right now" — a per-call,
    DB-backed routing fact, not a broken gate. It must behave exactly like
    an orchestrator built with ``gate=None``: pass, no regeneration, and
    nothing on the scrape — counting it as ``pass`` (or worse, fail-open)
    would make a judge-less deployment look reviewed."""
    counters = OutputQualityCounters()
    gate = _Gate([NoveltyVerdict.pass_unrouted()])
    orchestrator = _orchestrator(gate, counters)

    review, calls = await _review(
        gate, policy=policy, orchestrator=orchestrator,
    )

    assert review.outcome == OUTCOME_PASS
    assert review.final == "原稿"
    assert review.regen_attempted is False
    assert calls == []
    assert counters.snapshot() == {}


async def test_unrouted_re_review_fails_open_on_the_retry() -> None:
    """The judge route vanished between the two calls (an admin flipping
    providers mid-review). Same disposal as a gate that broke: ship the
    draft written in response to the feedback, count the fail-open."""
    gate = _Gate([_HARD, NoveltyVerdict.pass_unrouted()])

    review, _calls = await _review(gate, policy=BACKGROUND)

    assert review.outcome == OUTCOME_GATE_ERROR_FAILOPEN
    assert review.final == "重生稿"


@pytest.mark.parametrize("policy", [BACKGROUND, CHAT])
async def test_a_raising_judge_fails_open_rather_than_blocking(policy) -> None:
    """A gate that cannot answer must never become a new way for a message
    to die — on either policy, including the one that is willing to skip."""
    gate = _Gate([], raises=True)

    review, calls = await _review(gate, policy=policy)

    assert review.outcome == OUTCOME_GATE_ERROR_FAILOPEN
    assert review.final == "原稿"
    assert calls == []


@pytest.mark.parametrize("policy", [BACKGROUND, CHAT])
async def test_pass_open_with_an_error_is_not_counted_as_a_pass(policy) -> None:
    """``pass_open`` ships like a pass and must *count* like a failure to
    judge: an unreviewed message that reads as reviewed is precisely the
    blind spot the fail-open alarm exists to remove."""
    gate = _Gate([NoveltyVerdict.pass_open("model returned junk")])

    review, _ = await _review(gate, policy=policy)

    assert review.outcome == OUTCOME_GATE_ERROR_FAILOPEN
    assert review.final == "原稿"


# ── background: fail closed, with a re-review ────────────────────────


async def test_background_hard_failure_recovered_by_the_regeneration() -> None:
    gate = _Gate([_HARD, _PASS])

    review, calls = await _review(gate, policy=BACKGROUND)

    assert review.outcome == OUTCOME_HARD_RECOVERED
    assert review.final == "重生稿"
    assert calls == ["正文混入 schema 片段"]
    assert gate.seen == ["原稿", "重生稿"]


async def test_background_soft_failure_recovered_by_the_regeneration() -> None:
    gate = _Gate([_SOFT, _PASS])

    review, _ = await _review(gate, policy=BACKGROUND)

    assert review.outcome == OUTCOME_SOFT_RECOVERED
    assert review.final == "重生稿"


async def test_background_hard_failure_that_survives_re_review_sends_nothing() -> None:
    """The heart of D1: reviewed, regenerated, reviewed again, still broken
    → this tick publishes nothing at all."""
    gate = _Gate([_HARD, _HARD])

    review, _ = await _review(gate, policy=BACKGROUND)

    assert review.outcome == OUTCOME_HARD_SKIPPED
    assert review.final is None
    assert review.skipped is True
    assert gate.seen == ["原稿", "重生稿"]


async def test_background_soft_failure_that_survives_re_review_still_ships() -> None:
    """Soft axes are opinions about quality. Withholding a whole post over
    one costs more than the post is worth, so the regenerated draft goes
    out anyway."""
    gate = _Gate([_SOFT, _SOFT])

    review, _ = await _review(gate, policy=BACKGROUND)

    assert review.outcome == OUTCOME_SOFT_PUBLISHED_BEST_EFFORT
    assert review.final == "重生稿"


async def test_background_soft_first_then_hard_on_re_review_sends_nothing() -> None:
    """The disposal follows the draft that would actually ship. A soft
    first verdict whose regeneration leaked structure is a hard skip — the
    player would have seen the *second* draft's defect."""
    gate = _Gate([_SOFT, _HARD])

    review, _ = await _review(gate, policy=BACKGROUND)

    assert review.outcome == OUTCOME_HARD_SKIPPED
    assert review.final is None


async def test_background_regeneration_failure_skips_on_hard_ships_on_soft() -> None:
    hard_gate = _Gate([_HARD])
    hard, _ = await _review(hard_gate, policy=BACKGROUND, retry=None)
    assert hard.outcome == OUTCOME_HARD_SKIPPED
    assert hard.final is None
    assert hard.regen_attempted is True
    # Nothing to re-review — the gate was asked exactly once.
    assert hard_gate.seen == ["原稿"]

    soft_gate = _Gate([_SOFT])
    soft, _ = await _review(soft_gate, policy=BACKGROUND, retry=None)
    assert soft.outcome == OUTCOME_SOFT_PUBLISHED_BEST_EFFORT
    assert soft.final == "原稿"


async def test_background_blank_regeneration_counts_as_no_regeneration() -> None:
    gate = _Gate([_HARD])

    review, _ = await _review(gate, policy=BACKGROUND, retry="   ")

    assert review.outcome == OUTCOME_HARD_SKIPPED
    assert review.final is None


async def test_background_regeneration_that_raises_is_a_disposal_not_a_crash() -> None:
    gate = _Gate([_SOFT])
    orchestrator = _orchestrator(gate)

    async def regenerate(_feedback: str):
        raise RuntimeError("composer exploded")

    review = await orchestrator.review(
        "原稿",
        surface="feed",
        context_for=_context,
        regenerate=regenerate,
        policy=BACKGROUND,
    )

    assert review.outcome == OUTCOME_SOFT_PUBLISHED_BEST_EFFORT
    assert review.final == "原稿"


async def test_background_re_review_gate_error_ships_the_regenerated_draft() -> None:
    """The judge broke between the two calls. Fail open on the draft the
    model wrote *in response to* the feedback rather than fail closed on a
    broken judge."""
    class _SecondCallRaises(_Gate):
        async def evaluate(self, context, *, character=None):
            self.seen.append(context.response_text)
            if len(self.seen) == 1:
                return _HARD
            raise RuntimeError("judge down")

    gate = _SecondCallRaises([])

    review, _ = await _review(gate, policy=BACKGROUND)

    assert review.outcome == OUTCOME_GATE_ERROR_FAILOPEN
    assert review.final == "重生稿"


# ── the degrade exception (D1) ───────────────────────────────────────
#
# A caller that can drop the broken part and keep the rest declares which
# hard axes it survives that way. The matrix below is what keeps the
# exception from eating the rule: it applies to background only, only when
# nothing outside the declared set fired, and it must never be recorded as
# the hard_skipped that alarms.


async def test_degradable_axis_hands_the_draft_back_instead_of_skipping() -> None:
    counters = OutputQualityCounters()
    gate = _Gate([_TOOL, _TOOL])

    review, _ = await _review(
        gate,
        policy=BACKGROUND,
        orchestrator=_orchestrator(gate, counters),
        degrade_axes=_DEGRADABLE,
    )

    assert review.outcome == OUTCOME_HARD_DEGRADED
    assert review.final == "重生稿"
    assert review.skipped is False
    assert counters.snapshot() == {("feed", OUTCOME_HARD_DEGRADED): 1}
    assert counters.total("feed", OUTCOME_HARD_SKIPPED) == 0


async def test_degrade_is_all_or_nothing_across_the_fired_hard_axes() -> None:
    """A draft that leaked structure *and* broke its tool prompt is still a
    skip — dropping the picture would publish the leak."""
    review, _ = await _review(
        _Gate([_TOOL_AND_LEAK, _TOOL_AND_LEAK]),
        policy=BACKGROUND,
        degrade_axes=_DEGRADABLE,
    )

    assert review.outcome == OUTCOME_HARD_SKIPPED
    assert review.final is None


async def test_degrade_without_a_declared_axis_set_is_still_a_skip() -> None:
    """Every surface that has no rescue keeps 寧靜勿爛 unchanged."""
    review, _ = await _review(_Gate([_TOOL, _TOOL]), policy=BACKGROUND)

    assert review.outcome == OUTCOME_HARD_SKIPPED
    assert review.final is None


async def test_degrade_applies_when_there_was_no_second_draft() -> None:
    """Regeneration produced nothing, so the first draft is what there is.
    Withholding it would cost the post over a defect the caller can drop."""
    review, _ = await _review(
        _Gate([_TOOL]),
        policy=BACKGROUND,
        retry=None,
        degrade_axes=_DEGRADABLE,
    )

    assert review.outcome == OUTCOME_HARD_DEGRADED
    assert review.final == "原稿"


async def test_degrade_applies_with_no_retry_budget() -> None:
    review, calls = await _review(
        _Gate([_TOOL]),
        policy=BACKGROUND,
        max_retries=0,
        degrade_axes=_DEGRADABLE,
    )

    assert review.outcome == OUTCOME_HARD_DEGRADED
    assert review.final == "原稿"
    assert calls == []


async def test_degrade_never_applies_to_a_recovered_draft() -> None:
    """The exception is a disposal, not a shortcut: a regeneration that
    passes is an ordinary recovery and must read as one."""
    review, _ = await _review(
        _Gate([_TOOL, _PASS]), policy=BACKGROUND, degrade_axes=_DEGRADABLE,
    )

    assert review.outcome == OUTCOME_HARD_RECOVERED
    assert review.final == "重生稿"


async def test_degrade_axes_do_not_reach_the_chat_policy() -> None:
    """Chat already ships everything; a degrade there would only relabel
    the outcome and lose the "a player saw a defect" signal."""
    review, _ = await _review(
        _Gate([_TOOL]), policy=CHAT, degrade_axes=_DEGRADABLE,
    )

    assert review.outcome == OUTCOME_HARD_PUBLISHED_BEST_EFFORT
    assert review.final == "重生稿"


async def test_a_soft_failure_is_untouched_by_degrade_axes() -> None:
    review, _ = await _review(
        _Gate([_SOFT, _SOFT]), policy=BACKGROUND, degrade_axes=_DEGRADABLE,
    )

    assert review.outcome == OUTCOME_SOFT_PUBLISHED_BEST_EFFORT
    assert review.final == "重生稿"


# ── chat: never silent, never re-reviewed ────────────────────────────


async def test_chat_does_not_re_review_the_regenerated_draft() -> None:
    """The 2026-06-17 D5 latency decision, pinned. A player is watching the
    typing indicator; a second judge call before delivery is a second
    round-trip they wait through."""
    gate = _Gate([_HARD])

    review, calls = await _review(gate, policy=CHAT)

    assert gate.seen == ["原稿"]
    assert calls == ["正文混入 schema 片段"]
    assert review.final == "重生稿"
    assert review.outcome == OUTCOME_HARD_PUBLISHED_BEST_EFFORT


async def test_chat_soft_failure_ships_the_regenerated_draft() -> None:
    gate = _Gate([_SOFT])

    review, _ = await _review(gate, policy=CHAT)

    assert review.outcome == OUTCOME_SOFT_PUBLISHED_BEST_EFFORT
    assert review.final == "重生稿"


@pytest.mark.parametrize("retry", [None, "", "   "])
async def test_chat_falls_back_to_the_original_when_regeneration_gives_nothing(
    retry,
) -> None:
    """Chat has no "send nothing" move: an unusable retry means the first
    draft ships, defects and all."""
    gate = _Gate([_HARD])

    review, _ = await _review(gate, policy=CHAT, retry=retry)

    assert review.final == "原稿"
    assert review.outcome == OUTCOME_HARD_PUBLISHED_BEST_EFFORT


# ── no retry budget ──────────────────────────────────────────────────


async def test_zero_retries_background_skips_hard_and_ships_soft() -> None:
    hard_gate = _Gate([_HARD])
    hard, calls = await _review(hard_gate, policy=BACKGROUND, max_retries=0)
    assert hard.outcome == OUTCOME_HARD_SKIPPED
    assert hard.final is None
    assert hard.regen_attempted is False
    assert calls == []

    soft, _ = await _review(_Gate([_SOFT]), policy=BACKGROUND, max_retries=0)
    assert soft.outcome == OUTCOME_SOFT_PUBLISHED_BEST_EFFORT
    assert soft.final == "原稿"


async def test_zero_retries_chat_always_ships_the_original() -> None:
    hard, _ = await _review(_Gate([_HARD]), policy=CHAT, max_retries=0)
    assert hard.outcome == OUTCOME_HARD_PUBLISHED_BEST_EFFORT
    assert hard.final == "原稿"

    soft, _ = await _review(_Gate([_SOFT]), policy=CHAT, max_retries=0)
    assert soft.outcome == OUTCOME_SOFT_PUBLISHED_BEST_EFFORT
    assert soft.final == "原稿"


async def test_no_regenerate_callback_behaves_like_zero_retries() -> None:
    review, _ = await _review(_Gate([_HARD]), policy=BACKGROUND, retry=_NO_REGEN)
    assert review.outcome == OUTCOME_HARD_SKIPPED
    assert review.regen_attempted is False


# ── wiring-off and bookkeeping ───────────────────────────────────────


async def test_an_unwired_gate_passes_without_counting_anything() -> None:
    """"No gate" is not "reviewed and clean". Folding them together would
    make a mis-wired deployment read as immaculate on the scrape."""
    counters = OutputQualityCounters()
    orchestrator = OutputQualityOrchestrator(gate=None, counters=counters)

    review = await orchestrator.review(
        "原稿", surface="feed", context_for=_context,
    )

    assert review.outcome == OUTCOME_PASS
    assert review.final == "原稿"
    assert counters.snapshot() == {}


async def test_disabled_flag_passes_without_calling_the_gate() -> None:
    gate = _Gate([_HARD])
    counters = OutputQualityCounters()

    review = await orchestrator_review_disabled(gate, counters)

    assert review.outcome == OUTCOME_PASS
    assert gate.seen == []
    assert counters.snapshot() == {}


async def orchestrator_review_disabled(gate, counters):
    orchestrator = OutputQualityOrchestrator(gate=gate, counters=counters)
    return await orchestrator.review(
        "原稿", surface="feed", context_for=_context, enabled=False,
    )


async def test_a_context_builder_that_raises_fails_open() -> None:
    """Evidence assembly is caller code running inside this band; a bug in
    it must degrade to "unreviewed", not to "turn lost"."""
    gate = _Gate([_HARD])
    orchestrator = _orchestrator(gate)

    def boom(_candidate):
        raise ValueError("bad evidence")

    review = await orchestrator.review(
        "原稿", surface="feed", context_for=boom, policy=BACKGROUND,
    )

    assert review.outcome == OUTCOME_GATE_ERROR_FAILOPEN
    assert review.final == "原稿"
    assert gate.seen == []


async def test_every_exit_records_exactly_one_outcome() -> None:
    counters = OutputQualityCounters()
    orchestrator = _orchestrator(_Gate([_HARD, _HARD]), counters)

    async def regenerate(_feedback: str):
        return "重生稿"

    await orchestrator.review(
        "原稿",
        surface="feed",
        context_for=_context,
        regenerate=regenerate,
        policy=BACKGROUND,
    )

    assert counters.snapshot() == {("feed", OUTCOME_HARD_SKIPPED): 1}


async def test_counters_are_kept_per_surface() -> None:
    counters = OutputQualityCounters()
    for surface in ("feed", "proactive", "feed"):
        orchestrator = _orchestrator(_Gate([_PASS]), counters)
        await orchestrator.review(
            "原稿", surface=surface, context_for=_context,
        )

    assert counters.total("feed", OUTCOME_PASS) == 2
    assert counters.total("proactive", OUTCOME_PASS) == 1


async def test_the_review_carries_both_verdicts() -> None:
    review, _ = await _review(_Gate([_HARD, _SOFT]), policy=BACKGROUND)

    assert review.first_verdict is _HARD
    assert review.final_verdict is _SOFT


async def test_fired_axes_lists_hard_axes_first() -> None:
    verdict = NoveltyVerdict(
        passes=False, over_warm=True, visible_truncation=True,
    )

    assert fired_axes(verdict) == ("visible_truncation", "over_warm")
    assert fired_axes(None) == ()
    assert fired_axes(_PASS) == ()
