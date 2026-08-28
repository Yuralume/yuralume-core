"""The shared review→regenerate→dispose band (D7).

One object, two policies, seven outcomes. Everything specific to a surface
— how to build a gate context, how to ask that surface's composer for
another draft — arrives as a callback, so this module never learns what a
feed post or an encounter transcript is, and a new surface costs a caller
change rather than a branch here.

**Why the two policies differ.** The disposal table (D1) is not a style
preference; the two sides pay different costs for the same mistake.

*Background* surfaces (feed, proactive, follow-up, promise, encounter,
起幕) publish into a stream nobody is waiting on. Sending nothing this tick
costs one missing post that the next tick replaces; sending a reply with a
leaked schema tag or a half-finished sentence costs the illusion the whole
product is made of. So a hard failure that survives its regeneration ends
the tick: :data:`OUTCOME_HARD_SKIPPED`, ``final=None``. Because nothing is
waiting, background surfaces can also afford to **re-review** the
regenerated draft — otherwise "regenerate once" would be a ritual, not a
gate. The one exception is a surface that can *drop* the broken part and
keep the rest, which it declares as ``degrade_axes``; see
:data:`OUTCOME_HARD_DEGRADED`.

*Chat* cannot do either. A player is watching the typing indicator, and
"send nothing" is not one of the moves — so a hard failure ships the best
draft available (:data:`OUTCOME_HARD_PUBLISHED_BEST_EFFORT`) and the
regenerated draft is **not** re-reviewed, preserving the 2026-06-17 D5
latency call.

**Fail-open is not a pass.** A judge that raised, timed out or returned
unparseable JSON produces :data:`OUTCOME_GATE_ERROR_FAILOPEN` and the
original candidate ships. That is deliberate — a broken gate must not stop
the character talking — and it is also the failure mode nobody would
notice, which is why it is counted separately and alarms on a streak (see
:mod:`~kokoro_link.application.services.output_quality.counters`) rather
than folding into ``pass``.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from typing import Generic, TypeVar

from kokoro_link.application.services.output_quality.counters import (
    OutputQualityCounters,
)
from kokoro_link.contracts.novelty_gate import (
    HARD_AXES as _HARD_AXES,
    NoveltyGateContext,
    NoveltyGatePort,
    NoveltyVerdict,
)
from kokoro_link.domain.entities.character import Character

_LOGGER = logging.getLogger(__name__)

T = TypeVar("T")

# -- outcome vocabulary ----------------------------------------------------
#
# These strings are a published interface: they are the ``outcome`` label
# on the Prometheus series, the word in the structured log an operator
# greps for, and the value every adopting surface asserts on in its own
# tests. Renaming one silently breaks dashboards and alert rules that this
# repo cannot see. Add, never rename.

OUTCOME_PASS = "pass"
"""Cleared on the first review. Nothing was regenerated."""

OUTCOME_SOFT_PUBLISHED_BEST_EFFORT = "soft_published_best_effort"
"""A soft failure survived (or could not be) regenerated, and shipped
anyway. The five soft axes are quality opinions; withholding a whole post
over one would cost more than the post is worth."""

OUTCOME_SOFT_RECOVERED = "soft_recovered"
"""Background only: a soft failure regenerated into a draft that passed
re-review. The gate working as designed."""

OUTCOME_HARD_RECOVERED = "hard_recovered"
"""Background only: a hard failure regenerated into a clean draft."""

OUTCOME_HARD_SKIPPED = "hard_skipped"
"""ALERT LINE. A hard failure survived its regeneration on a background
surface, so this tick sent **nothing** (``final is None``). Ordinary in
ones; sustained non-zero means players are silently losing posts and
messages — the exact shape of the 2026-08-26 incident."""

OUTCOME_HARD_PUBLISHED_BEST_EFFORT = "hard_published_best_effort"
"""Chat only: a hard failure shipped because a player was waiting. Every
one of these is a player who saw a defect; the chat-side lever is D3's
sticky risk escalation, not withholding the reply."""

OUTCOME_GATE_ERROR_FAILOPEN = "gate_error_failopen"
"""No verdict could be had — the candidate shipped unreviewed. See the
module docstring; the streak alarm lives in ``counters``."""

OUTCOME_HARD_DEGRADED = "hard_degraded"
"""The D1 exception: a surface-specific rescue that beats both shipping
and skipping. A caller that can *drop* the broken part and keep the rest
— feed, whose text-only degrade discards a defective image prompt and
posts the clean prose — declares the axes it can survive that way via
``degrade_axes``; when the surviving hard failure fires nothing outside
that set, this is the outcome, ``final`` is the draft to degrade, and no
``hard_skipped`` is recorded. Keeping the two apart is the whole point:
``hard_skipped`` is an alert line, and a post that published without its
picture must not ring it."""

ALL_OUTCOMES: tuple[str, ...] = (
    OUTCOME_PASS,
    OUTCOME_SOFT_PUBLISHED_BEST_EFFORT,
    OUTCOME_SOFT_RECOVERED,
    OUTCOME_HARD_RECOVERED,
    OUTCOME_HARD_SKIPPED,
    OUTCOME_HARD_PUBLISHED_BEST_EFFORT,
    OUTCOME_GATE_ERROR_FAILOPEN,
    OUTCOME_HARD_DEGRADED,
)

_FEEDBACK_LOG_CHARS = 120


class OutputQualityPolicy(str, Enum):
    """Which half of the D1 disposal table a surface plays by."""

    BACKGROUND_FAIL_CLOSED = "background_fail_closed"
    """Nobody is waiting: re-review the regeneration, and send nothing
    rather than send a hard defect."""

    CHAT_BEST_EFFORT = "chat_best_effort"
    """Somebody is waiting: one regeneration, no re-review, always send."""


@dataclass(frozen=True, slots=True)
class OutputQualityReview(Generic[T]):
    """What the caller acts on.

    ``final is None`` has exactly one meaning and it is the same on every
    surface: **send nothing this tick**. Callers map it onto whatever
    "nothing" already means for them (an empty string, a skipped row, an
    un-consumed quota) rather than inventing a new refusal path.
    """

    final: T | None
    outcome: str
    first_verdict: NoveltyVerdict | None = None
    final_verdict: NoveltyVerdict | None = None
    """The re-review's verdict on background surfaces; the same object as
    ``first_verdict`` when nothing was re-reviewed."""
    regen_attempted: bool = False

    @property
    def skipped(self) -> bool:
        return self.final is None


def fired_axes(verdict: NoveltyVerdict | None) -> tuple[str, ...]:
    """Names of the axes that fired, hard ones first; ``()`` for no verdict.

    The None-tolerance is the whole reason this wrapper still exists next
    to :attr:`NoveltyVerdict.fired_axes` — callers here routinely hold a
    verdict that was never produced.
    """
    return () if verdict is None else verdict.fired_axes


class OutputQualityOrchestrator:
    """Reviews one candidate, regenerates it once, decides what ships."""

    __slots__ = ("_counters", "_gate")

    def __init__(
        self,
        *,
        gate: NoveltyGatePort | None = None,
        counters: OutputQualityCounters | None = None,
    ) -> None:
        self._gate = gate
        self._counters = counters or OutputQualityCounters()

    @property
    def counters(self) -> OutputQualityCounters:
        return self._counters

    @property
    def gate(self) -> NoveltyGatePort | None:
        return self._gate

    async def review(
        self,
        candidate: T,
        *,
        surface: str,
        context_for: Callable[[T], NoveltyGateContext],
        regenerate: Callable[[str], Awaitable[T | None]] | None = None,
        policy: OutputQualityPolicy = OutputQualityPolicy.BACKGROUND_FAIL_CLOSED,
        character: Character | None = None,
        max_retries: int = 1,
        enabled: bool = True,
        fallback_feedback: str = "",
        degrade_axes: frozenset[str] = frozenset(),
    ) -> OutputQualityReview[T]:
        """Run the whole band for one candidate. Never raises.

        *context_for* is called with the candidate being reviewed — the
        original first, and on a background re-review the regenerated one
        — so a caller that recomputes mechanical evidence per draft (the
        length-overrun line, for instance) gets it right without this
        module knowing what evidence is.

        *regenerate* receives the judge's feedback verbatim and returns a
        fresh candidate, or ``None`` when it could not produce one.
        Omitting it (or ``max_retries<=0``) means "review only": the
        disposal table still applies, there is simply no second draft.

        *degrade_axes* names the hard axes this surface can survive by
        *dropping something* rather than by withholding the whole message
        (feed's text-only degrade). Background only, and only when the
        surviving failure fires nothing outside the set: the disposal then
        hands the draft back as :data:`OUTCOME_HARD_DEGRADED` instead of
        skipping. Left empty — every other surface — nothing changes.

        With no gate wired, or ``enabled=False``, this returns ``pass``
        without a model call **and without counting** — an unwired gate is
        not a review that passed, and folding the two together would make
        a mis-wired deployment look immaculate on the scrape. The same
        holds when the gate answers :meth:`NoveltyVerdict.pass_unrouted`:
        provider routing is DB-backed and mutable at runtime, so "is there
        a judge at all" is the gate's per-call answer, not a wiring fact —
        an unrouted call renders no indicators either.
        """
        if not enabled or self._gate is None:
            return OutputQualityReview(final=candidate, outcome=OUTCOME_PASS)

        first = await self._evaluate(candidate, context_for, character, surface)
        if first is not None and first.unrouted:
            return OutputQualityReview(final=candidate, outcome=OUTCOME_PASS)
        if first is None or _is_pass_open_error(first):
            return self._finish(
                surface=surface,
                review=OutputQualityReview(
                    final=candidate,
                    outcome=OUTCOME_GATE_ERROR_FAILOPEN,
                    first_verdict=first,
                    final_verdict=first,
                ),
                character=character,
            )
        if first.passes:
            return self._finish(
                surface=surface,
                review=OutputQualityReview(
                    final=candidate,
                    outcome=OUTCOME_PASS,
                    first_verdict=first,
                    final_verdict=first,
                ),
                character=character,
            )

        hard = first.hard_fail
        feedback = first.feedback or fallback_feedback or _DEFAULT_FEEDBACK
        if regenerate is None or max_retries <= 0:
            if hard and _is_background(policy):
                final, outcome = _hard_disposal(candidate, first, degrade_axes)
            else:
                final = candidate
                outcome = (
                    OUTCOME_HARD_PUBLISHED_BEST_EFFORT if hard
                    else OUTCOME_SOFT_PUBLISHED_BEST_EFFORT
                )
            return self._finish(
                surface=surface,
                review=OutputQualityReview(
                    final=final,
                    outcome=outcome,
                    first_verdict=first,
                    final_verdict=first,
                ),
                character=character,
            )

        retry = await self._regenerate(regenerate, feedback, surface, character)
        if _is_background(policy):
            review = await self._dispose_background(
                candidate=candidate,
                retry=retry,
                first=first,
                hard=hard,
                context_for=context_for,
                character=character,
                surface=surface,
                degrade_axes=degrade_axes,
            )
        else:
            review = self._dispose_chat(
                candidate=candidate, retry=retry, first=first, hard=hard,
            )
        return self._finish(surface=surface, review=review, character=character)

    # -- disposal ---------------------------------------------------------

    async def _dispose_background(
        self,
        *,
        candidate: T,
        retry: T | None,
        first: NoveltyVerdict,
        hard: bool,
        context_for: Callable[[T], NoveltyGateContext],
        character: Character | None,
        surface: str,
        degrade_axes: frozenset[str] = frozenset(),
    ) -> OutputQualityReview[T]:
        """D1's background row: re-review, then fail closed on hard."""
        if retry is None or _is_blank(retry):
            # No second draft. A hard defect with no replacement is the one
            # case where sending nothing is strictly better than sending —
            # unless the surface can drop the broken part and keep the rest.
            if hard:
                final, outcome = _hard_disposal(candidate, first, degrade_axes)
            else:
                final, outcome = candidate, OUTCOME_SOFT_PUBLISHED_BEST_EFFORT
            return OutputQualityReview(
                final=final,
                outcome=outcome,
                first_verdict=first,
                final_verdict=first,
                regen_attempted=True,
            )
        second = await self._evaluate(retry, context_for, character, surface)
        if second is None or _is_pass_open_error(second) or second.unrouted:
            # The gate broke between the two calls (or lost its judge route
            # mid-review). Fail open on the draft
            # the model wrote *in response to* the feedback: it is the more
            # likely of the two to be clean, and withholding it would be
            # fail-closing on a broken judge rather than on a bad draft.
            return OutputQualityReview(
                final=retry,
                outcome=OUTCOME_GATE_ERROR_FAILOPEN,
                first_verdict=first,
                final_verdict=second,
                regen_attempted=True,
            )
        if second.passes:
            return OutputQualityReview(
                final=retry,
                outcome=OUTCOME_HARD_RECOVERED if hard else OUTCOME_SOFT_RECOVERED,
                first_verdict=first,
                final_verdict=second,
                regen_attempted=True,
            )
        if second.hard_fail:
            # Note the axis is read off the *re-review*: a first-pass soft
            # failure whose regeneration leaked a schema tag is a hard skip,
            # because what ships is the second draft's defect, not the
            # first's. Same reason the degrade is judged on ``second``.
            final, outcome = _hard_disposal(retry, second, degrade_axes)
            return OutputQualityReview(
                final=final,
                outcome=outcome,
                first_verdict=first,
                final_verdict=second,
                regen_attempted=True,
            )
        return OutputQualityReview(
            final=retry,
            outcome=OUTCOME_SOFT_PUBLISHED_BEST_EFFORT,
            first_verdict=first,
            final_verdict=second,
            regen_attempted=True,
        )

    def _dispose_chat(
        self,
        *,
        candidate: T,
        retry: T | None,
        first: NoveltyVerdict,
        hard: bool,
    ) -> OutputQualityReview[T]:
        """D1's chat row: one regeneration, no re-review, always send."""
        usable = retry is not None and not _is_blank(retry)
        return OutputQualityReview(
            final=retry if usable else candidate,
            outcome=(
                OUTCOME_HARD_PUBLISHED_BEST_EFFORT if hard
                else OUTCOME_SOFT_PUBLISHED_BEST_EFFORT
            ),
            first_verdict=first,
            final_verdict=first,
            regen_attempted=True,
        )

    # -- collaborator calls, all of which must not raise ------------------

    async def _evaluate(
        self,
        candidate: T,
        context_for: Callable[[T], NoveltyGateContext],
        character: Character | None,
        surface: str,
    ) -> NoveltyVerdict | None:
        gate = self._gate
        if gate is None:
            return None
        try:
            context = context_for(candidate)
        except Exception:  # noqa: BLE001 - evidence assembly must not kill a turn
            _LOGGER.exception(
                "output quality: context builder raised surface=%s", surface,
            )
            return None
        try:
            return await gate.evaluate(context, character=character)
        except Exception:  # noqa: BLE001 - the port promises not to; assume it does
            _LOGGER.exception(
                "output quality: gate raised surface=%s", surface,
            )
            return None

    async def _regenerate(
        self,
        regenerate: Callable[[str], Awaitable[T | None]],
        feedback: str,
        surface: str,
        character: Character | None,
    ) -> T | None:
        try:
            return await regenerate(feedback)
        except Exception:  # noqa: BLE001 - a failed retry is a disposal, not a crash
            _LOGGER.exception(
                "output quality: regeneration raised surface=%s character=%s",
                surface, getattr(character, "id", "?"),
            )
            return None

    # -- bookkeeping ------------------------------------------------------

    def _finish(
        self,
        *,
        surface: str,
        review: OutputQualityReview[T],
        character: Character | None,
    ) -> OutputQualityReview[T]:
        """Count it and log it, once, wherever the branch above ended.

        Every exit goes through here so a caller never has to remember to
        report — the reason the pre-QG surfaces threw their verdicts away
        was that reporting was each caller's job.
        """
        try:
            self._counters.record(surface, review.outcome)
        except Exception:  # noqa: BLE001 - a counter must not break a turn
            _LOGGER.exception("output quality: counter record failed")
        verdict = review.final_verdict or review.first_verdict
        axes = fired_axes(verdict) or fired_axes(review.first_verdict)
        feedback = ((verdict.feedback if verdict else "") or "")[:_FEEDBACK_LOG_CHARS]
        log = _LOGGER.info if review.outcome == OUTCOME_PASS else _LOGGER.warning
        log(
            "output quality: surface=%s outcome=%s axes=%s regen=%s "
            "character=%s feedback=%s",
            surface,
            review.outcome,
            ",".join(axes) or "-",
            review.regen_attempted,
            getattr(character, "id", "?"),
            feedback or "-",
        )
        return review


_DEFAULT_FEEDBACK = "內容未通過玩家可見輸出品質檢查，請重寫。"


def _is_background(policy: OutputQualityPolicy) -> bool:
    return policy is OutputQualityPolicy.BACKGROUND_FAIL_CLOSED


def _hard_disposal(
    candidate: T, verdict: NoveltyVerdict, degrade_axes: frozenset[str],
) -> tuple[T | None, str]:
    """Send nothing, or hand back the draft for the surface's own degrade.

    The degrade is deliberately all-or-nothing: it applies only when
    *every* hard axis that fired is one the caller said it can drop. A
    draft that leaked a schema tag *and* wrote a broken image prompt is
    still a skip — dropping the picture would publish the leak.
    """
    if degrade_axes:
        fired = {
            axis for axis in _HARD_AXES if getattr(verdict, axis, False)
        }
        if fired and fired <= degrade_axes:
            return candidate, OUTCOME_HARD_DEGRADED
    return None, OUTCOME_HARD_SKIPPED


def _is_pass_open_error(verdict: NoveltyVerdict) -> bool:
    """Is this a ``pass_open`` carrying an error reason?

    ``NoveltyVerdict.pass_open`` is how the adapters spell "I could not
    judge this" — it passes, so treating it as a pass would be *correct*
    for delivery and wrong for observability: the whole point of the
    fail-open counter is that it is distinguishable from a real pass.
    """
    metadata = verdict.gate_metadata or {}
    return bool(verdict.passes and metadata.get("error"))


def _is_blank(candidate: object) -> bool:
    """Is this candidate empty enough to be worse than the original?

    Only strings can answer honestly. A surface whose candidate is a
    structured object knows its own emptiness rules and is expected to
    return ``None`` from ``regenerate`` rather than an empty object.
    """
    if isinstance(candidate, str):
        return not candidate.strip()
    return False


__all__ = [
    "ALL_OUTCOMES",
    "OUTCOME_GATE_ERROR_FAILOPEN",
    "OUTCOME_HARD_DEGRADED",
    "OUTCOME_HARD_PUBLISHED_BEST_EFFORT",
    "OUTCOME_HARD_RECOVERED",
    "OUTCOME_HARD_SKIPPED",
    "OUTCOME_PASS",
    "OUTCOME_SOFT_PUBLISHED_BEST_EFFORT",
    "OUTCOME_SOFT_RECOVERED",
    "OutputQualityOrchestrator",
    "OutputQualityPolicy",
    "OutputQualityReview",
    "fired_axes",
]
