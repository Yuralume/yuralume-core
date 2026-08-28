"""The outcome-claim honesty gate, as the application layer sees it (HV1).

:class:`ComposerToolLoop` asks one question — "may this text go out?" —
and must not also own retry counting, alarm thresholds or a metrics
surface. That is what this service is: the thin band between the loop
and :class:`OutcomeClaimJudgePort` where the *operational* rules live.

Three of them, and each exists because of a specific way this gate can go
wrong in production:

**A judge that cannot answer is not an approval.** ``unavailable`` is
returned as itself, never softened into ``consistent``. The caller's
fail direction (park, for every background path — §3.4) is the caller's
to apply, but it can only apply it if the distinction survives this
layer.

**A judge outage must be loud.** Fail-closed means an upstream that goes
down does not break anything visibly — it quietly parks every promise on
the deployment, forever, while the log fills with ordinary per-call
warnings. So consecutive failures are counted, and crossing
:data:`DEFAULT_FAILURE_ALARM_STREAK` logs ``ERROR`` **once per crossing**
and raises a counter that reads as an alert line on the scrape. One
success resets the streak: an intermittent failure is not an outage.

**The gate never becomes a new way for a turn to die.** The port's
contract says it does not raise; this layer assumes it anyway, because a
gate that can throw would turn a model hiccup into a lost promise —
strictly worse than the dishonesty it exists to stop.

**Every call here also feeds the HV3 per-round audit trail** (see
:mod:`kokoro_link.application.services.outcome_claim_audit`) — a
:class:`~contextvars.ContextVar`-scoped event list a caller can open around
one round to recover its verdict(s), block count, correction, and park
reason for a ``TurnRecord``. Unconditional and free of cost when no caller
has opened a scope: the event functions are a dict-lookup no-op outside
one, so this file needs no flag to stay byte-identical for callers that
never adopt it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from kokoro_link.application.services.outcome_claim_audit import (
    record_block_event,
    record_corrected_event,
    record_judge_event,
    record_parked_event,
)
from kokoro_link.contracts.outcome_claim import (
    OutcomeClaimEvidence,
    OutcomeClaimJudgePort,
    OutcomeClaimVerdict,
)
from kokoro_link.domain.entities.character import Character

_LOGGER = logging.getLogger(__name__)

DEFAULT_FAILURE_ALARM_STREAK = 3
"""Consecutive judge failures before the outage alarm fires.

Three, not one: a single timeout is ordinary weather for any provider and
costs exactly one parked tick, while three in a row is the shape of an
upstream that is actually down. Configurable per deployment because the
right number depends on how often the seam runs, which depends on how
many promises the deployment carries."""


@dataclass(slots=True)
class OutcomeClaimCounters:
    """In-process totals, lifted onto the metrics scrape (AP5 pattern).

    Deliberately counts *outcomes of the gate*, not model calls: the
    question an operator asks of this seam is "how often is the character
    claiming things it did not do", and the answer has to survive a
    restart-free week of log rotation.
    """

    reviewed: int = 0
    """Messages that reached the gate with something to check."""
    consistent: int = 0
    consistent_truncated: int = 0
    """S5. Of ``consistent``, the ones the judge only reviewed a *prefix*
    of (the message ran past the judge's message-length cap — currently
    the rare tail on chat, whose replies carry no composer-side cap at
    all). Not a block: the visible part read clean and the verdict ships
    as ``consistent`` exactly as it always has. This counter is the trace
    that answer must never lack — before S5 an unseen tail could read as
    a fully-reviewed pass with nothing anywhere to show it wasn't.
    Sustained non-zero says the cap in ``outcome_claim_honesty`` is biting
    often enough to be worth raising, not that anything is currently
    wrong."""
    blocked_zero_call: int = 0
    """Blocked at the exit where the composer called no tool at all — the
    production shape from 2026-08-25."""
    blocked_after_tools: int = 0
    """Blocked at the pass-2 exit: a tool ran, the prose overstated it.

    S7: also carries the pass-2 park-that-actually-shipped case — a tool
    had already produced deliverable attachments, so ``ComposerToolLoop``
    ships them with a fixed fallback line instead of sending nothing.
    That round was not "empty" (see ``parked``'s docstring), so it counts
    here instead — a single verdict is still only counted once even when
    the correction that followed it also failed to ship (``_park``'s
    ``already_blocked`` guards the second occurrence)."""
    corrected: int = 0
    """Re-runs that came back honest and shipped. The gate working."""
    parked: int = 0
    """Rounds that sent NOTHING this tick because the re-run reoffended
    or no verdict could be had — the composers' retry-next-tick, not a
    promise broken. S7: a round whose tool call already produced
    deliverable attachments never lands here, even when the gate blocked
    the prose, because those attachments still ship with a fallback line
    — see ``blocked_after_tools``. Before S7 this counter (and the
    matching park log line) fired unconditionally and therefore lied
    about exactly those rounds."""
    park_retries_exhausted: int = 0
    """F1 ALERT LINE. Promises abandoned because the model re-offended on
    every one of the row's allowed retries. A park is a promise deferred;
    this is the one place a promise is actually **dropped**, so it must
    never be inferable only from a row quietly vanishing from the queue.
    Sustained non-zero means the honesty prompt rules are losing badly
    enough that whole fulfilments are being written off."""
    judge_failed: int = 0
    """Individual verdicts that could not be produced."""
    judge_outage: int = 0
    """ALERT LINE. Times a failure streak crossed the alarm threshold.
    Non-zero means promises are being parked by an unreachable judge, not
    by dishonest text."""
    chat_audited: int = 0
    """HV4. Chat turns that offered tools and reached the post-turn audit.
    The denominator the chat-side dishonesty rate divides by — the
    background counters above cannot serve as one, because chat is judged
    *after* delivery and never blocks."""
    chat_repair_queued: int = 0
    """HV4. Durable repair follow-ups written after a chat turn was found
    dishonest. The character will come back and settle the claim."""
    chat_repair_coalesced: int = 0
    """F5. Caught lies folded into a repair the conversation already owed
    instead of opening a second row.

    Counted separately from ``chat_repair_queued`` so the two questions
    stay separable: how often the character lied (queued + coalesced) and
    how many times it will come back to say so (queued). A coalesce rate
    approaching the queue rate is the signature of a capability that is
    failing repeatedly rather than of a model that occasionally
    overclaims — which is a different incident with a different fix."""
    chat_repair_missed: int = 0
    """HV4 ALERT LINE. The judge found a chat turn dishonest and the
    repair row did **not** land — no follow-up store on this deployment,
    or the write raised. Every one of these is a lie the player was told
    and nobody is going to come back and own, which is precisely the
    outcome D6's "100%" exists to forbid; it must never be inferred from
    the gap between ``chat_repair_queued`` and the log."""


class OutcomeClaimGuard:
    """Ask the judge, count what happened, alarm on an outage."""

    __slots__ = ("_judge", "_alarm_streak", "_counters", "_failure_streak")

    def __init__(
        self,
        *,
        judge: OutcomeClaimJudgePort,
        failure_alarm_streak: int = DEFAULT_FAILURE_ALARM_STREAK,
    ) -> None:
        self._judge = judge
        self._alarm_streak = max(1, failure_alarm_streak)
        self._counters = OutcomeClaimCounters()
        # Kept off the counters dataclass on purpose: that object is
        # enumerated field-by-field onto the Prometheus scrape, and a
        # streak is a transient the exporter has no use for.
        self._failure_streak = 0

    @property
    def counters(self) -> OutcomeClaimCounters:
        return self._counters

    async def review(
        self,
        *,
        message_text: str,
        evidence: OutcomeClaimEvidence,
        character: Character | None = None,
        operator_primary_language: str = "",
    ) -> OutcomeClaimVerdict:
        """Verdict for one about-to-ship message. Never raises.

        Empty text is answered ``consistent`` without a model call: there
        is nothing to claim, and the caller already has its own meaning
        for an empty body ("retry next tick")."""
        text = (message_text or "").strip()
        if not text:
            return OutcomeClaimVerdict.ok()
        self._counters.reviewed += 1
        try:
            verdict = await self._judge.judge(
                message_text=text,
                evidence=evidence,
                character=character,
                operator_primary_language=operator_primary_language,
            )
        except Exception:  # noqa: BLE001 - a gate must not kill the turn
            _LOGGER.exception(
                "outcome-claim guard: judge raised character=%s — "
                "treating as a judge failure",
                getattr(character, "id", "?"),
            )
            verdict = OutcomeClaimVerdict.failed()
        # HV3: one event per verdict actually produced, whichever of the
        # three states it landed on — recorded before either branch below
        # returns, so a caller's audit scope sees every judge call this
        # round made, not just the ones that happened to ship.
        record_judge_event(
            status=verdict.status,
            unsupported_claim_count=len(verdict.unsupported_claims),
            truncated=verdict.truncated,
        )
        if verdict.unavailable:
            self._record_failure(character)
            return verdict
        self._failure_streak = 0
        if verdict.consistent:
            self._counters.consistent += 1
            if verdict.truncated:
                # S5: the status is unchanged — the visible prefix read
                # clean — but this must never be indistinguishable from a
                # verdict over the full message. The counter above is the
                # trace; this line is the same trace in the log an
                # operator is actually looking at when they go digging.
                self._counters.consistent_truncated += 1
                _LOGGER.warning(
                    "outcome-claim guard: verdict consistent but the "
                    "message was truncated before the judge saw it "
                    "character=%s — the unseen tail was never reviewed",
                    getattr(character, "id", "?"),
                )
        return verdict

    # -- bookkeeping the loop reports back into ---------------------------

    def record_block(self, *, after_tools: bool) -> None:
        """A verdict came back inconsistent and a re-run was ordered."""
        if after_tools:
            self._counters.blocked_after_tools += 1
        else:
            self._counters.blocked_zero_call += 1
        record_block_event(after_tools=after_tools)

    def record_corrected(self) -> None:
        """The re-run produced something the judge accepted."""
        self._counters.corrected += 1
        record_corrected_event()

    def record_parked(self, *, reason: str = "", park_kind: str = "") -> None:
        """The round ended without sending: re-run reoffended, or the
        judge never answered.

        ``reason`` is additive (HV3) — every pre-HV3 call site keeps
        working with the default. It carries no message content, only the
        loop's own short static phrase (see
        ``ComposerToolLoop._park``), so it is safe for the audit trail.

        ``park_kind`` (F1) is that phrase's machine-readable class, so the
        caller that has to *do* something different about a lying model
        than about a broken judge can branch on it instead of matching on
        the prose. Also defaulted, for the same reason."""
        self._counters.parked += 1
        record_parked_event(reason=reason, park_kind=park_kind)

    def record_park_retries_exhausted(self) -> None:
        """A parked promise hit its retry ceiling and was given up on.

        See :attr:`OutcomeClaimCounters.park_retries_exhausted` — this is
        the counter half of "絕不無聲"; the caller also logs ``ERROR``."""
        self._counters.park_retries_exhausted += 1

    # -- HV4 chat post-turn audit bookkeeping -----------------------------
    #
    # Counters rather than audit events: the ContextVar trail exists to
    # reconstruct one *composition round's* verdict chain, and the chat
    # audit has exactly one verdict and no re-compose. What the operator
    # needs from this seam is the rate, and the rate belongs on the same
    # dataclass as the background one so a deployment reads as a single
    # honesty story instead of two half-stories.

    def record_chat_audit(self) -> None:
        """A chat turn reached the post-turn audit."""
        self._counters.chat_audited += 1

    def record_chat_repair_queued(self) -> None:
        """A durable repair follow-up landed for a dishonest chat turn."""
        self._counters.chat_repair_queued += 1

    def record_chat_repair_coalesced(self) -> None:
        """A caught lie joined a repair row the conversation already had.

        Still an owed claim, just not a second row — see the counter's
        docstring for why it is not folded into ``chat_repair_queued``."""
        self._counters.chat_repair_coalesced += 1

    def record_chat_repair_missed(self) -> None:
        """A dishonest chat turn produced **no** repair row. See the
        counter's docstring — this is an alert line, not a statistic."""
        self._counters.chat_repair_missed += 1

    def _record_failure(self, character: Character | None) -> None:
        self._counters.judge_failed += 1
        self._failure_streak += 1
        streak = self._failure_streak
        if streak < self._alarm_streak:
            _LOGGER.warning(
                "outcome-claim guard: no verdict character=%s (streak=%d) — "
                "failing closed", getattr(character, "id", "?"), streak,
            )
            return
        if streak == self._alarm_streak:
            self._counters.judge_outage += 1
            _LOGGER.error(
                "outcome-claim guard: %d consecutive judge failures — every "
                "promise fulfilment on this process is now being parked "
                "unsent. The honesty gate's model route is the suspect, not "
                "the composers. Check the %s feature route.",
                streak, "outcome_claim_judge",
            )
            return
        _LOGGER.error(
            "outcome-claim guard: judge still failing (streak=%d) — promises "
            "remain parked", streak,
        )


__all__ = [
    "DEFAULT_FAILURE_ALARM_STREAK",
    "OutcomeClaimCounters",
    "OutcomeClaimGuard",
]
