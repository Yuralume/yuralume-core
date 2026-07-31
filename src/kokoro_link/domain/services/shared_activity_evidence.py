"""Evidence rules for "did the operator actually take part in this?".

A schedule activity may carry an operator ``ParticipantRef`` — the
character planned to do it *with the user*. Whether that plan actually
happened is a separate question, and answering it wrong is a
gaslight-class bug: the 7/27 case wrote "和木木一起跑到那間說好要來的
刨冰店，終於吃到啦" into memory and onto the public wall for an invite
the user never accepted and never showed up for. Weeks later the
character brought that "shared memory" up in a proactive message.

This module owns the **structured** half of the fix. It answers two
questions from schema data only (participant role + the single
cross-source interaction timestamp the turn pipeline maintains):

1. In what state was the commitment? (``operator_role``)
2. Is there any evidence the operator was around while it ran?
   (:class:`OperatorPresenceEvidence`)

and folds them into one policy verdict (:class:`SharedClaimPolicy`) that
downstream prompt builders hand to the LLM. The *wording* of the
resulting memory / post is never decided here — per the project's top
directive, we supply facts and a rule, and the model writes the text.

Deliberately conservative: absence of evidence is never read as evidence
of presence. ``UNVERIFIED`` ("they agreed, but nothing proves they were
there") and ``SOLO_ONLY`` ("they never agreed at all") both forbid a
"we did it together" claim; only ``VERIFIED`` permits one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum

from kokoro_link.domain.entities.schedule import (
    OPERATOR_ANY_INVOLVEMENT_ROLES,
    OPERATOR_CONFIRMED_LAPSED_ROLE,
    OPERATOR_CONFIRMED_SHARED_ROLE,
    OPERATOR_INVITE_EXPIRED_ROLE,
    OPERATOR_INVITE_PENDING_ROLE,
    OPERATOR_WISH_ROLE,
    ScheduleActivity,
)


class SharedClaimPolicy(StrEnum):
    """What a narrator (aftermath memory / feed post) may claim."""

    NOT_APPLICABLE = "not_applicable"
    """No operator attached — an ordinary solo activity, no extra rule."""

    SOLO_ONLY = "solo_only"
    """The operator never agreed (a pending invite, an expired invite, or
    a private wish). The activity is the character's alone; a shared
    completion claim is factually false, not merely unproven."""

    UNVERIFIED = "unverified"
    """The operator did agree, but nothing available at judging time shows
    they were actually present. Neither "we did it" nor "they stood me up"
    may be asserted."""

    VERIFIED = "verified"
    """The operator agreed *and* interacted with the character while the
    activity was running — a shared framing is allowed."""


class OperatorPresenceEvidence(StrEnum):
    """What the interaction timestamp says about the activity's window.

    The anchor is ``CharacterState.last_active_at`` — the single
    cross-source "user last spoke" instant the turn pipeline maintains
    (the same anchor the feed's silence collector and proactive idle
    computation use). Being a *last* timestamp rather than a log, it can
    only ever prove presence, never absence-after-the-fact: a user who
    chatted during the block and again afterwards reads as
    ``ONLY_AFTER``. That asymmetry is intentional — the failure mode is
    under-claiming, which is the safe direction.
    """

    UNKNOWN = "unknown"
    """No interaction history reachable from this call site at all."""

    NEVER = "never"
    """The user has never spoken to this character."""

    BEFORE_ACTIVITY = "before_activity"
    """Their last message predates the block — they sent nothing during it."""

    DURING_ACTIVITY = "during_activity"
    """They spoke while the block was running. The one positive signal."""

    ONLY_AFTER = "only_after"
    """Their last message lands after the block ended, so whether they
    were around during it is unknowable from this anchor."""


_SOLO_ONLY_ROLES = frozenset({
    OPERATOR_INVITE_PENDING_ROLE,
    OPERATOR_INVITE_EXPIRED_ROLE,
    OPERATOR_WISH_ROLE,
})
"""Roles that never carried the operator's agreement. ``expired`` sits
here for the same reason CF3 put it in the memorialiser's guard: the
sweep renaming a pending invite must not quietly open a door the pending
state kept shut."""

_AGREED_ROLES = frozenset({
    OPERATOR_CONFIRMED_SHARED_ROLE,
    OPERATOR_CONFIRMED_LAPSED_ROLE,
})
"""Roles where the operator said yes at some point. ``lapsed`` is the
swept form of ``confirmed_shared`` and gets the same evidence test — an
agreement alone was never proof of attendance."""


@dataclass(frozen=True, slots=True)
class SharedActivityEvidence:
    """Structured verdict handed to prompt builders.

    Every field is schema-derived: participant role values and a
    timestamp comparison. Nothing here inspects ``description`` or any
    other user-visible prose.
    """

    policy: SharedClaimPolicy = SharedClaimPolicy.NOT_APPLICABLE
    operator_role: str | None = None
    operator_display_name: str = ""
    presence: OperatorPresenceEvidence = OperatorPresenceEvidence.UNKNOWN

    @property
    def involves_operator(self) -> bool:
        return self.policy is not SharedClaimPolicy.NOT_APPLICABLE

    @property
    def may_claim_shared_completion(self) -> bool:
        return self.policy is SharedClaimPolicy.VERIFIED


def derive_shared_activity_evidence(
    activity: ScheduleActivity,
    *,
    last_active_at: datetime | None,
    interaction_history_known: bool = True,
) -> SharedActivityEvidence:
    """Read the operator's involvement in ``activity`` off structured data.

    ``last_active_at`` is ``CharacterState.last_active_at``.
    ``interaction_history_known`` distinguishes "the user has never
    spoken" (a fact) from "this call site cannot see the character state"
    (ignorance) — both block a shared claim, but only the first may be
    narrated as such.
    """
    ref = _operator_ref(activity)
    if ref is None:
        return SharedActivityEvidence()
    role = ref.role
    presence = _presence(
        activity,
        last_active_at=last_active_at,
        interaction_history_known=interaction_history_known,
    )
    if role in _SOLO_ONLY_ROLES:
        policy = SharedClaimPolicy.SOLO_ONLY
    elif role in _AGREED_ROLES:
        policy = (
            SharedClaimPolicy.VERIFIED
            if presence is OperatorPresenceEvidence.DURING_ACTIVITY
            else SharedClaimPolicy.UNVERIFIED
        )
    else:  # pragma: no cover - guarded by OPERATOR_ANY_INVOLVEMENT_ROLES
        return SharedActivityEvidence()
    return SharedActivityEvidence(
        policy=policy,
        operator_role=role,
        operator_display_name=(ref.display_name or "").strip(),
        presence=presence,
    )


def _operator_ref(activity: ScheduleActivity):  # noqa: ANN202 - ParticipantRef | None
    for ref in activity.participant_refs:
        if ref.actor_kind != "operator":
            continue
        if ref.role in OPERATOR_ANY_INVOLVEMENT_ROLES:
            return ref
    return None


def _presence(
    activity: ScheduleActivity,
    *,
    last_active_at: datetime | None,
    interaction_history_known: bool,
) -> OperatorPresenceEvidence:
    if not interaction_history_known:
        return OperatorPresenceEvidence.UNKNOWN
    if last_active_at is None:
        return OperatorPresenceEvidence.NEVER
    moment = (
        last_active_at
        if last_active_at.tzinfo is not None
        else last_active_at.replace(tzinfo=timezone.utc)
    ).astimezone(timezone.utc)
    if moment < activity.start_at:
        return OperatorPresenceEvidence.BEFORE_ACTIVITY
    if moment < activity.end_at:
        return OperatorPresenceEvidence.DURING_ACTIVITY
    return OperatorPresenceEvidence.ONLY_AFTER
