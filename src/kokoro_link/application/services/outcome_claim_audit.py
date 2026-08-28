"""Per-round audit trail for the outcome-claim honesty gate (HV3).

HV1's :class:`~kokoro_link.application.services.outcome_claim_guard.
OutcomeClaimGuard` counts *aggregate* totals for the process
(``counters.blocked_zero_call`` and friends) — enough to answer "is this
happening", never "which round". HV3 exists to answer the second question:
a specific pending-follow-up / scheduled-promise round's judge verdict(s),
whether the gate blocked it, whether a retry corrected it, and — the case
that otherwise leaves NO trace at all — why it was parked, so a dishonesty
rate (謊稱率) has something to divide by other than silence.

Why a :class:`~contextvars.ContextVar` rather than a new parameter
-------------------------------------------------------------------
:class:`OutcomeClaimGuard` is shared, process-lifetime, and its
``review`` / ``record_block`` / ``record_corrected`` / ``record_parked``
methods are called from deep inside
:class:`~kokoro_link.application.services.composer_tool_loop.
ComposerToolLoop` — a file this ticket must not edit (HV2 is in it in
parallel). Threading a "which round is this" identifier through every one
of those call sites would mean changing that loop's signatures for a
feature it does not otherwise need to know about.

A :class:`ContextVar` sidesteps that entirely, the same way
``interaction_context`` and ``generation_trigger`` already do in this
codebase: a caller opens :func:`outcome_claim_audit_scope` around the one
``await`` chain that composes and judges a single round, the guard's
existing calls (unmodified call sites, bar the one described below) drop
their events into whatever scope is active, and the caller reads them back
with :func:`outcome_claim_audit_summary` once the chain returns. Two
concurrent rounds — different characters, different asyncio Tasks — never
see each other's events: each ``asyncio.Task`` runs inside its own copy of
the context, which is exactly the isolation this needs and the reason a
plain module-level list would not do.

Outside a scope (no caller has opted in — chat, or a caller that predates
HV3) the guard's calls become one dict lookup that finds nothing and
returns immediately. Zero overhead, zero behaviour change.

Events, not fields
-------------------
One round can consult the judge more than once — a zero-call offence that
gets corrected by a real tool call rejoins the ordinary tool path and asks
the judge again after the tool runs. Trying to fold that into a single
"the verdict was X" record either loses the first offence or requires
threading a round id back through the guard's argument-less bookkeeping
calls. An ordered list of small typed events (``judge`` / ``block`` /
``corrected`` / ``parked``) records exactly what happened, in order, with
no guessing about which call belongs to which; :func:`outcome_claim_audit_summary`
folds it into the flat shape a ``TurnRecord.post_turn_refs`` wants.

Log de-identification redline
------------------------------
Events never carry ``message_text`` or the judge's ``unsupported_claims``
phrases — only counts and the enum status string. The existing
``_LOGGER.warning`` lines in ``composer_tool_loop`` already draw this same
line (``len(verdict.unsupported_claims)``, never the text), and a
persisted audit row is held to the same rule, not a looser one.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

OUTCOME_CLAIM_AUDIT_JUDGE = "judge"
"""One :meth:`OutcomeClaimGuard.review` call that actually reached the
judge (empty-text short-circuits are not recorded — there was nothing to
judge)."""

OUTCOME_CLAIM_AUDIT_BLOCK = "block"
"""A verdict came back inconsistent and a one-shot re-compose was ordered."""

OUTCOME_CLAIM_AUDIT_CORRECTED = "corrected"
"""The re-run came back honest — real tool call or judge-cleared text."""

OUTCOME_CLAIM_AUDIT_PARKED = "parked"
"""The round ended without sending anything this tick."""


OUTCOME_CLAIM_PARK_MODEL_REOFFENDED = "model_reoffended"
"""The gate worked and the *model* lost: a verdict came back inconsistent
and the one correction pass could not produce an honest message (it
re-claimed, wrote nothing, or the payload had no channel to carry the
correction through). Repeating the round is a fresh sample of the same
model on the same prompt — worth a bounded number of tries, never an
unbounded one, because a systematically dishonest row would otherwise
re-release forever (D5: 間隔下限＋總次數上限)."""

OUTCOME_CLAIM_PARK_JUDGE_UNAVAILABLE = "judge_unavailable"
"""**Our** judge could not answer. Nothing is known about the message's
honesty, so this says nothing about the model and must never be allowed
to confiscate a promise the player is owed: a caller backs the retry off
in time, and does not count it against any attempt budget."""

OUTCOME_CLAIM_PARK_AUTONOMOUS_SILENCE = "autonomous_silence"
"""Shown its own overclaim, the model chose not to answer at all —
``should_send=False`` or an empty message from the correction pass. This
is one of the two honest roads out of an inconsistent verdict (the other
is a real tool call, or a judge-cleared retry), not a further offence: the
model was given the chance to keep talking and *declined* to, rather than
trying again and failing. A caller that folded this into
:data:`OUTCOME_CLAIM_PARK_MODEL_REOFFENDED` would count honest restraint
as a second lie."""


@dataclass(frozen=True, slots=True)
class OutcomeClaimParkReason:
    """One park cause, as the pair it has to be.

    ``phrase`` is the short static text that reaches logs and the audit
    row; ``kind`` is the machine-readable class a caller branches on. They
    live glued together because they are two views of one decision, and
    the alternative — a caller re-deriving the class by matching on the
    phrase — is a string comparison that would silently pick the wrong
    branch the day somebody reworded a log line.
    """

    phrase: str
    kind: str


PARK_NO_VERDICT_ZERO_CALL = OutcomeClaimParkReason(
    "no verdict at the zero-call exit", OUTCOME_CLAIM_PARK_JUDGE_UNAVAILABLE,
)
PARK_NO_VERDICT_AFTER_TOOLS = OutcomeClaimParkReason(
    "no verdict after tools", OUTCOME_CLAIM_PARK_JUDGE_UNAVAILABLE,
)
PARK_NO_CORRECTION_CHANNEL = OutcomeClaimParkReason(
    "payload cannot carry a correction", OUTCOME_CLAIM_PARK_MODEL_REOFFENDED,
)
PARK_CORRECTION_WROTE_NOTHING = OutcomeClaimParkReason(
    "correction pass wrote nothing", OUTCOME_CLAIM_PARK_MODEL_REOFFENDED,
)
PARK_CORRECTION_CLAIMED_AGAIN = OutcomeClaimParkReason(
    "correction pass claimed an outcome again",
    OUTCOME_CLAIM_PARK_MODEL_REOFFENDED,
)
PARK_CORRECTION_OVERCLAIMED_AGAIN = OutcomeClaimParkReason(
    "correction pass overclaimed again", OUTCOME_CLAIM_PARK_MODEL_REOFFENDED,
)
"""The six ways :class:`~kokoro_link.application.services.composer_tool_loop.
ComposerToolLoop` can end a round unsent. The four
:data:`OUTCOME_CLAIM_PARK_MODEL_REOFFENDED` ones all begin with an
*inconsistent* verdict — the model did claim something the evidence did
not support — and differ only in how the correction failed, which is why
they share one class and one attempt budget."""


PARK_PROACTIVE_JUDGE_UNAVAILABLE = OutcomeClaimParkReason(
    "no verdict on the proactive tick", OUTCOME_CLAIM_PARK_JUDGE_UNAVAILABLE,
)
PARK_PROACTIVE_OVERCLAIMED_AFTER_TOOLS = OutcomeClaimParkReason(
    "overclaimed after tools ran", OUTCOME_CLAIM_PARK_MODEL_REOFFENDED,
)
PARK_PROACTIVE_CORRECTION_RAISED = OutcomeClaimParkReason(
    "correction re-decide raised", OUTCOME_CLAIM_PARK_MODEL_REOFFENDED,
)
PARK_PROACTIVE_CORRECTION_SILENT = OutcomeClaimParkReason(
    "correction chose not to send", OUTCOME_CLAIM_PARK_AUTONOMOUS_SILENCE,
)
PARK_PROACTIVE_CORRECTION_OVERCLAIMED_AGAIN = OutcomeClaimParkReason(
    "correction overclaimed again", OUTCOME_CLAIM_PARK_MODEL_REOFFENDED,
)
"""The five ways :class:`~kokoro_link.application.services.
proactive_dispatcher.ProactiveDispatcher._resolve_outbound_honesty` can
end a tick unsent.

Three land on :data:`OUTCOME_CLAIM_PARK_MODEL_REOFFENDED` for the same
reason ``composer_tool_loop``'s do: each follows an *inconsistent* first
verdict (the model already claimed something the evidence did not
support), so a re-decide that dies before producing a clean message —
whether by claiming again (``…_OVERCLAIMED_AGAIN``), by overclaiming once
tools have already run and there is no second pass to spend
(``…_OVERCLAIMED_AFTER_TOOLS``), or by the re-decide call itself raising
(``…_CORRECTION_RAISED``) — is one more failed attempt to correct the same
offence, not a fresh one.

``…_CORRECTION_SILENT`` is deliberately *not* grouped with those three: a
re-decide that comes back with ``should_send=False`` took the honest
"say nothing" road rather than failing to take the honest "say it
correctly" road, which is why it gets
:data:`OUTCOME_CLAIM_PARK_AUTONOMOUS_SILENCE` instead — see that
constant's docstring.

``…_JUDGE_UNAVAILABLE`` is the odd one out for the reason
:data:`OUTCOME_CLAIM_PARK_JUDGE_UNAVAILABLE` always is: no verdict came
back at all, so nothing is known about the model, and it must not be
billed as one.

``PendingFollowUpDispatcher``'s attempt-budget classifier
(``_honesty_park_disposition``) does not read these — the proactive tick
has no attempt budget to charge (unlike a queued promise, a missed push
is not retried by this gate at all) — so this taxonomy exists for the
audit trail and dishonesty-rate accounting only, not to gate a retry."""


@dataclass(frozen=True, slots=True)
class OutcomeClaimAuditEvent:
    """One entry in a round's ordered trail. Fields outside a ``kind``'s
    relevance stay at their default rather than being made optional per
    kind — a flat shape is simpler to fold in :func:`outcome_claim_audit_summary`
    than a tagged union would be, and the unused defaults cost nothing."""

    kind: str
    status: str = ""
    """:data:`OUTCOME_CLAIM_AUDIT_JUDGE` only — the verdict's status string
    (``consistent`` / ``inconsistent`` / ``unavailable``)."""
    unsupported_claim_count: int = 0
    """:data:`OUTCOME_CLAIM_AUDIT_JUDGE` only. A count, never the phrases
    themselves — see the module docstring's log redline."""
    truncated: bool = False
    """:data:`OUTCOME_CLAIM_AUDIT_JUDGE` only (S5). Whether the judge saw
    only a prefix of the message. Folded into the summary regardless of
    ``status`` — a ``consistent`` verdict on a truncated message is still
    ``consistent``, but a reader of one round's trail must be able to
    tell "fully reviewed" from "prefix reviewed, prefix was clean" rather
    than have the second read as the first."""
    after_tools: bool | None = None
    """:data:`OUTCOME_CLAIM_AUDIT_BLOCK` only — which of the loop's two
    exits blocked (pass-1 zero-call vs pass-2 overclaim)."""
    reason: str = ""
    """:data:`OUTCOME_CLAIM_AUDIT_PARKED` only — a short static phrase from
    the loop (e.g. ``"no verdict after tools"``), never message content."""
    park_kind: str = ""
    """:data:`OUTCOME_CLAIM_AUDIT_PARKED` only — which class of park this
    was (:data:`OUTCOME_CLAIM_PARK_MODEL_REOFFENDED` /
    :data:`OUTCOME_CLAIM_PARK_JUDGE_UNAVAILABLE`). Empty for a caller that
    only had a phrase to give, which is answered as "unclassified" rather
    than guessed at."""


_CURRENT_AUDIT: ContextVar[list[OutcomeClaimAuditEvent] | None] = ContextVar(
    "yuralume_outcome_claim_audit", default=None,
)


@contextmanager
def outcome_claim_audit_scope() -> Iterator[None]:
    """Open a fresh, empty trail for the ``await`` chain inside the block.

    Nesting is not a supported shape (no caller in this codebase composes
    two rounds inside one another); a nested scope simply starts its own
    empty list and the outer one resumes seeing its own on exit, same as
    any ``ContextVar.set`` / ``reset`` pair.
    """
    token = _CURRENT_AUDIT.set([])
    try:
        yield
    finally:
        _CURRENT_AUDIT.reset(token)


def _record(event: OutcomeClaimAuditEvent) -> None:
    events = _CURRENT_AUDIT.get()
    if events is not None:
        events.append(event)


def record_judge_event(
    *,
    status: str,
    unsupported_claim_count: int = 0,
    truncated: bool = False,
) -> None:
    _record(OutcomeClaimAuditEvent(
        kind=OUTCOME_CLAIM_AUDIT_JUDGE,
        status=status,
        unsupported_claim_count=unsupported_claim_count,
        truncated=truncated,
    ))


def record_block_event(*, after_tools: bool) -> None:
    _record(OutcomeClaimAuditEvent(
        kind=OUTCOME_CLAIM_AUDIT_BLOCK, after_tools=after_tools,
    ))


def record_corrected_event() -> None:
    _record(OutcomeClaimAuditEvent(kind=OUTCOME_CLAIM_AUDIT_CORRECTED))


def record_parked_event(*, reason: str = "", park_kind: str = "") -> None:
    _record(OutcomeClaimAuditEvent(
        kind=OUTCOME_CLAIM_AUDIT_PARKED, reason=reason, park_kind=park_kind,
    ))


def current_outcome_claim_audit() -> tuple[OutcomeClaimAuditEvent, ...]:
    """The active scope's events so far, or ``()`` outside a scope."""
    events = _CURRENT_AUDIT.get()
    return tuple(events) if events is not None else ()


def outcome_claim_audit_summary() -> dict[str, object] | None:
    """Fold the active scope's trail into a flat, JSON-safe dict.

    ``None`` when there is no active scope, or the scope closed having
    never reached the judge (no tools offered, guard unwired) — the
    caller's cue to skip writing an audit row for a round the gate never
    touched. Intended to sit under a single ``post_turn_refs`` key (see
    ``PendingFollowUpDispatcher._record_honesty_audit``).
    """
    events = current_outcome_claim_audit()
    if not events:
        return None
    judge_events = [e for e in events if e.kind == OUTCOME_CLAIM_AUDIT_JUDGE]
    block_events = [e for e in events if e.kind == OUTCOME_CLAIM_AUDIT_BLOCK]
    parked_events = [e for e in events if e.kind == OUTCOME_CLAIM_AUDIT_PARKED]
    corrected_count = sum(
        1 for e in events if e.kind == OUTCOME_CLAIM_AUDIT_CORRECTED
    )
    return {
        "verdicts": [e.status for e in judge_events],
        "final_verdict": judge_events[-1].status if judge_events else "",
        "unsupported_claim_counts": [
            e.unsupported_claim_count for e in judge_events
        ],
        # S5: any verdict this round reached over only a prefix of the
        # message, not just the final one — one truncated-but-cleared
        # verdict followed by a corrected retry over the *full* text
        # (the retry carries the correction, not necessarily a shorter
        # message) still means this round had an unseen tail at some
        # point, which is the fact a reader must not lose.
        "any_truncated": any(e.truncated for e in judge_events),
        "blocked_count": len(block_events),
        "blocked_after_tools": any(
            bool(e.after_tools) for e in block_events
        ),
        "corrected_count": corrected_count,
        "parked": bool(parked_events),
        "parked_reason": parked_events[-1].reason if parked_events else "",
        # The class the *caller* branches on. Read off the same event the
        # phrase comes from so an audit row can never describe one park
        # with another's class.
        "parked_kind": parked_events[-1].park_kind if parked_events else "",
    }


def outcome_claim_park_kind(summary: dict[str, object] | None) -> str:
    """The park class in ``summary``, or ``""`` when it did not park.

    The one reader of the ``parked`` / ``parked_kind`` pair, so no caller
    has to remember that an absent ``parked`` makes ``parked_kind``
    meaningless — or that a summary can be ``None`` entirely (the round
    never reached the judge).
    """
    if not summary or not summary.get("parked"):
        return ""
    kind = summary.get("parked_kind")
    return kind if isinstance(kind, str) else ""


__all__ = [
    "OUTCOME_CLAIM_AUDIT_BLOCK",
    "OUTCOME_CLAIM_AUDIT_CORRECTED",
    "OUTCOME_CLAIM_AUDIT_JUDGE",
    "OUTCOME_CLAIM_AUDIT_PARKED",
    "OUTCOME_CLAIM_PARK_AUTONOMOUS_SILENCE",
    "OUTCOME_CLAIM_PARK_JUDGE_UNAVAILABLE",
    "OUTCOME_CLAIM_PARK_MODEL_REOFFENDED",
    "PARK_CORRECTION_CLAIMED_AGAIN",
    "PARK_CORRECTION_OVERCLAIMED_AGAIN",
    "PARK_CORRECTION_WROTE_NOTHING",
    "PARK_NO_CORRECTION_CHANNEL",
    "PARK_NO_VERDICT_AFTER_TOOLS",
    "PARK_NO_VERDICT_ZERO_CALL",
    "PARK_PROACTIVE_CORRECTION_OVERCLAIMED_AGAIN",
    "PARK_PROACTIVE_CORRECTION_RAISED",
    "PARK_PROACTIVE_CORRECTION_SILENT",
    "PARK_PROACTIVE_JUDGE_UNAVAILABLE",
    "PARK_PROACTIVE_OVERCLAIMED_AFTER_TOOLS",
    "OutcomeClaimAuditEvent",
    "OutcomeClaimParkReason",
    "current_outcome_claim_audit",
    "outcome_claim_audit_scope",
    "outcome_claim_audit_summary",
    "outcome_claim_park_kind",
    "record_block_event",
    "record_corrected_event",
    "record_judge_event",
    "record_parked_event",
]
