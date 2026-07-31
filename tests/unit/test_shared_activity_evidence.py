"""CF4 — the structured half of "did the operator actually take part?".

This is the schema-only layer: participant role values plus one
timestamp comparison. It decides *what may be claimed*, never what the
memory or the post says — that is the model's job, downstream.

The rules pinned here are the ones the 7/27 刨冰 incident violated: a
pending invite is not a shared outing, and an agreement alone is not
attendance.
"""

from __future__ import annotations

from datetime import datetime, timezone

from kokoro_link.domain.entities.schedule import (
    OPERATOR_CONFIRMED_LAPSED_ROLE,
    OPERATOR_CONFIRMED_SHARED_ROLE,
    OPERATOR_INVITE_EXPIRED_ROLE,
    OPERATOR_INVITE_PENDING_ROLE,
    OPERATOR_WISH_ROLE,
    ScheduleActivity,
)
from kokoro_link.domain.services.shared_activity_evidence import (
    OperatorPresenceEvidence,
    SharedClaimPolicy,
    derive_shared_activity_evidence,
)
from kokoro_link.domain.value_objects.actor import ParticipantRef

UTC = timezone.utc

_START = datetime(2026, 7, 27, 14, 0, tzinfo=UTC)
_END = datetime(2026, 7, 27, 15, 0, tzinfo=UTC)


def _activity(role: str | None, *, name: str = "木木") -> ScheduleActivity:
    refs = (
        (
            ParticipantRef(
                actor_kind="operator",
                actor_id=None,
                display_name=name,
                role=role,
            ),
        )
        if role is not None
        else ()
    )
    return ScheduleActivity.create(
        start_at=_START,
        end_at=_END,
        description="與木木一起前往先前約定的刨冰店，吃刨冰",
        category="social",
        participant_refs=refs,
    )


class TestPolicy:
    def test_no_operator_ref_is_not_applicable(self) -> None:
        evidence = derive_shared_activity_evidence(
            _activity(None), last_active_at=None,
        )
        assert evidence.policy is SharedClaimPolicy.NOT_APPLICABLE
        assert evidence.involves_operator is False

    def test_pending_invite_is_solo_only_even_with_interaction(self) -> None:
        """The 7/27 case. The user chatting during the slot does not turn an
        invite they never accepted into a shared outing."""
        evidence = derive_shared_activity_evidence(
            _activity(OPERATOR_INVITE_PENDING_ROLE),
            last_active_at=datetime(2026, 7, 27, 14, 30, tzinfo=UTC),
        )
        assert evidence.policy is SharedClaimPolicy.SOLO_ONLY
        assert evidence.may_claim_shared_completion is False

    def test_expired_invite_is_solo_only(self) -> None:
        """CF3's sweep renames the role; the guard must follow it."""
        evidence = derive_shared_activity_evidence(
            _activity(OPERATOR_INVITE_EXPIRED_ROLE), last_active_at=None,
        )
        assert evidence.policy is SharedClaimPolicy.SOLO_ONLY

    def test_wish_is_solo_only(self) -> None:
        evidence = derive_shared_activity_evidence(
            _activity(OPERATOR_WISH_ROLE), last_active_at=None,
        )
        assert evidence.policy is SharedClaimPolicy.SOLO_ONLY

    def test_confirmed_without_interaction_is_unverified(self) -> None:
        evidence = derive_shared_activity_evidence(
            _activity(OPERATOR_CONFIRMED_SHARED_ROLE),
            last_active_at=datetime(2026, 7, 26, 20, 0, tzinfo=UTC),
        )
        assert evidence.policy is SharedClaimPolicy.UNVERIFIED
        assert evidence.may_claim_shared_completion is False

    def test_confirmed_with_interaction_in_window_is_verified(self) -> None:
        evidence = derive_shared_activity_evidence(
            _activity(OPERATOR_CONFIRMED_SHARED_ROLE),
            last_active_at=datetime(2026, 7, 27, 14, 30, tzinfo=UTC),
        )
        assert evidence.policy is SharedClaimPolicy.VERIFIED
        assert evidence.may_claim_shared_completion is True
        assert evidence.operator_display_name == "木木"

    def test_lapsed_confirmation_gets_the_same_evidence_test(self) -> None:
        """A swept ``confirmed_shared`` is still an agreement — and an
        agreement was never proof of attendance."""
        assert (
            derive_shared_activity_evidence(
                _activity(OPERATOR_CONFIRMED_LAPSED_ROLE), last_active_at=None,
            ).policy
            is SharedClaimPolicy.UNVERIFIED
        )
        assert (
            derive_shared_activity_evidence(
                _activity(OPERATOR_CONFIRMED_LAPSED_ROLE),
                last_active_at=datetime(2026, 7, 27, 14, 5, tzinfo=UTC),
            ).policy
            is SharedClaimPolicy.VERIFIED
        )


class TestPresence:
    def test_unreachable_history_is_unknown_not_absence(self) -> None:
        """A call site that can't see the character state knows nothing —
        which must not be narrated as "they never showed up"."""
        evidence = derive_shared_activity_evidence(
            _activity(OPERATOR_CONFIRMED_SHARED_ROLE),
            last_active_at=None,
            interaction_history_known=False,
        )
        assert evidence.presence is OperatorPresenceEvidence.UNKNOWN

    def test_never_spoke(self) -> None:
        evidence = derive_shared_activity_evidence(
            _activity(OPERATOR_INVITE_PENDING_ROLE), last_active_at=None,
        )
        assert evidence.presence is OperatorPresenceEvidence.NEVER

    def test_last_message_before_the_block(self) -> None:
        evidence = derive_shared_activity_evidence(
            _activity(OPERATOR_INVITE_PENDING_ROLE),
            last_active_at=datetime(2026, 7, 26, 22, 0, tzinfo=UTC),
        )
        assert evidence.presence is OperatorPresenceEvidence.BEFORE_ACTIVITY

    def test_last_message_after_the_block_is_only_after(self) -> None:
        """A *last* timestamp cannot rule presence in or out once it moves
        past the block — the honest answer is "unknowable"."""
        evidence = derive_shared_activity_evidence(
            _activity(OPERATOR_CONFIRMED_SHARED_ROLE),
            last_active_at=datetime(2026, 7, 27, 18, 0, tzinfo=UTC),
        )
        assert evidence.presence is OperatorPresenceEvidence.ONLY_AFTER
        assert evidence.policy is SharedClaimPolicy.UNVERIFIED

    def test_end_boundary_is_exclusive(self) -> None:
        evidence = derive_shared_activity_evidence(
            _activity(OPERATOR_CONFIRMED_SHARED_ROLE), last_active_at=_END,
        )
        assert evidence.presence is OperatorPresenceEvidence.ONLY_AFTER

    def test_naive_timestamp_is_read_as_utc(self) -> None:
        evidence = derive_shared_activity_evidence(
            _activity(OPERATOR_CONFIRMED_SHARED_ROLE),
            last_active_at=datetime(2026, 7, 27, 14, 30),
        )
        assert evidence.presence is OperatorPresenceEvidence.DURING_ACTIVITY
