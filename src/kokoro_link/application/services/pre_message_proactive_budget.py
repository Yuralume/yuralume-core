"""Mechanical bounds on proactive pushes sent before the player's first word.

Two rules live here, both introduced by the TR series and both evaluated
in the same dispatcher branch: a **budget** (TR2-A — how many, how far
apart) and a **delay window** (TR2-B — not before the character has
existed for a while). The module keeps its budget-flavoured name because
that is what the first rule was; read it as "the mechanical floor under
the pre-message window", of which the budget is one half.

Neither rule ever schedules anything. They only remove eligibility, and
they sit in the cheap band of the gate — no model call is spent on a
tick they refuse.

TR2-A — anti-nag budget for pushes sent before the player's first word.

Why this exists (TRIAL_INSIGHTS_DEFAULTS_PLAN §2): the dispatcher's
anti-nag signal is :func:`_count_unanswered_streak`, and it returns 0
whenever ``idle_minutes`` is ``None`` — i.e. for exactly the players who
have never said anything. That is deliberate for its own purpose
("silence from a user who never spoke is not being ignored"), but it
means the whole unanswered-streak restraint is **absent** for the
pre-message case. Once ``proactive_permission`` becomes an opt-out
default (TR2-B), a player who creates a character and walks away would
be pinged on every eligible tick forever, with nothing anywhere counting
those pushes.

So the pre-message window gets its own, much blunter budget: a hard
**total** number of pushes allowed before the player's first message, and
a **minimum spacing** between two of them. Both are ceilings, not
schedules — they only ever remove eligibility; when and whether to speak
inside the remaining budget stays a semantic decision of the intention
judge / decider, exactly as before.

The budget stops participating the moment the player speaks: from then
on the character has real conversational signal and the normal cadence
(cooldown, daily limit, quiet hours, unanswered streak) is the restraint.
Nothing here is persisted — the ``proactive_attempt`` audit log already
records every SENT push, and before the first user message *every* SENT
row for the character is by definition a pre-message one.
"""

from collections.abc import Sequence
from datetime import datetime

from kokoro_link.contracts.clock import ensure_utc
from kokoro_link.contracts.proactive import GateVerdict
from kokoro_link.domain.entities.proactive_attempt import ProactiveAttempt

#: How many proactive pushes a character may send before the player has
#: ever spoken. Two is "a hello, and one gentle nudge later" — enough to
#: be an activation tool, few enough that walking away from the product
#: costs the player at most two notifications.
PRE_MESSAGE_PROACTIVE_CAP = 2

#: Minimum spacing between two pre-message pushes. A day is deliberately
#: coarse: without a reply there is no signal that could justify a
#: tighter rhythm, and the normal cooldown (minutes) is sized for a
#: conversation that is actually happening.
PRE_MESSAGE_PROACTIVE_MIN_INTERVAL_HOURS = 24.0

#: TR2-B — how long after the character was created the pre-message
#: window stays shut. Eight hours is chosen so the first contact still
#: lands inside the player's own day (activation is where the funnel
#: leaks, so "tomorrow" is too late) while never reading as an automated
#: reply to the create button: eight hours from any creation moment is a
#: different part of the day, which is what makes the character's opening
#: line feel like its own initiative rather than a welcome mail. Quiet
#: hours still apply on top, so a window that expires at 3am waits.
PRE_MESSAGE_PROACTIVE_DELAY_HOURS = 8.0

#: Gate reasons. Kept as prefixes (the parenthetical carries the numbers)
#: so downstream surfaces can recognise the case without parsing counts.
PRE_MESSAGE_CAP_REASON = "pre-message proactive cap reached"
PRE_MESSAGE_INTERVAL_REASON = "pre-message proactive interval active"
PRE_MESSAGE_BUDGET_UNAVAILABLE_REASON = "pre-message proactive budget unavailable"
PRE_MESSAGE_DELAY_REASON = "pre-message proactive delay window"


def evaluate_pre_message_proactive_delay(
    character_created_at: datetime | None,
    *,
    now: datetime,
    delay_hours: float = PRE_MESSAGE_PROACTIVE_DELAY_HOURS,
) -> GateVerdict:
    """TR2-B — has the character existed long enough to reach out first?

    With ``proactive_permission`` pre-checked in the creation form, the
    permission no longer marks a moment the player deliberately chose;
    it is simply the default they left alone. What used to be implied by
    the deliberate tick — "yes, and I know what I'm agreeing to" — is now
    carried by this floor instead: whatever the character eventually
    says, it will not arrive while the player is still on the page that
    created it.

    The anchor is **character creation**, not the seed's own timestamp.
    "Don't answer the create button" is the thing being prevented, and a
    player who opens an existing character months later and ticks the box
    by hand has just made exactly the deliberate choice this floor stands
    in for — making them wait would be treating an explicit act as if it
    were a default.

    A missing ``created_at`` means the entity never round-tripped through
    a row that carries the column (no-DB rigs and freshly-constructed
    entities), which is the opposite of "just created": the rule that
    needs the anchor simply does not fire, matching how the freeze
    reaper and the idle down-shift treat the same missing anchor. The
    push is not thereby unbounded — the TR2-A budget above still counts
    and spaces every one of them.
    """
    if character_created_at is None:
        return GateVerdict(
            passed=True, reason=f"{PRE_MESSAGE_DELAY_REASON} (no creation anchor)",
        )
    elapsed_hours = (
        ensure_utc(now) - ensure_utc(character_created_at)
    ).total_seconds() / 3600.0
    # Clock skew (negative elapsed) reads as "no time has passed", which
    # blocks — the conservative direction for a "not yet" rule.
    if elapsed_hours < delay_hours:
        remaining = delay_hours - elapsed_hours
        return GateVerdict(
            passed=False,
            reason=(
                f"{PRE_MESSAGE_DELAY_REASON} "
                f"({remaining:.1f}h left of {delay_hours:.0f}h since creation)"
            ),
        )
    return GateVerdict(
        passed=True, reason=f"{PRE_MESSAGE_DELAY_REASON} elapsed",
    )


def evaluate_pre_message_proactive_budget(
    sent_attempts: Sequence[ProactiveAttempt],
    *,
    now: datetime,
    cap: int = PRE_MESSAGE_PROACTIVE_CAP,
    min_interval_hours: float = PRE_MESSAGE_PROACTIVE_MIN_INTERVAL_HOURS,
) -> GateVerdict:
    """Decide whether one more pre-message push fits in the budget.

    ``sent_attempts`` is the character's SENT proactive attempts, newest
    first. The caller may truncate the fetch, but **must** fetch at least
    ``cap`` rows: the count is only read to compare against the ceiling,
    while the newest row is what the spacing is measured from.

    Returns a :class:`GateVerdict` in the same shape and register as the
    heuristic gate's own verdicts, so the dispatcher can log the reason
    through the identical GATE_BLOCKED path.
    """
    sent_count = len(sent_attempts)
    if sent_count >= cap:
        return GateVerdict(
            passed=False,
            reason=(
                f"{PRE_MESSAGE_CAP_REASON} "
                f"({sent_count}/{cap} before first user message)"
            ),
        )

    latest = _latest_decided_at(sent_attempts)
    if latest is not None:
        elapsed_hours = (ensure_utc(now) - latest).total_seconds() / 3600.0
        # Clock skew (negative elapsed) reads as "no time has passed",
        # which blocks — the conservative direction for an anti-nag rule.
        if elapsed_hours < min_interval_hours:
            remaining = min_interval_hours - elapsed_hours
            return GateVerdict(
                passed=False,
                reason=(
                    f"{PRE_MESSAGE_INTERVAL_REASON} "
                    f"({remaining:.1f}h left of {min_interval_hours:.0f}h)"
                ),
            )

    return GateVerdict(
        passed=True,
        reason=f"pre-message proactive budget ({sent_count}/{cap})",
    )


def _latest_decided_at(
    sent_attempts: Sequence[ProactiveAttempt],
) -> datetime | None:
    """Newest ``decided_at`` among the rows, as aware UTC.

    Does not trust the caller's ordering: the port documents newest-first
    but an in-memory / test double that returns insertion order would
    otherwise silently measure the spacing from the oldest push.
    """
    stamps = [
        ensure_utc(attempt.decided_at)
        for attempt in sent_attempts
        if attempt.decided_at is not None
    ]
    return max(stamps) if stamps else None
