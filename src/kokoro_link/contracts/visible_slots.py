"""Port for the visible-output slot claim ledger (P3-Dedup §3.4/§6.2).

A *slot* is an at-most-once claim on one visible per-character background
output for one tick bucket. The first executor to :meth:`claim` a
``(character_id, kind, slot)`` triple owns it and proceeds to compose /
deliver; a second executor racing the same slot (a reclaimed distributed
job vs a still-running original) fails the claim and MUST skip — never
raise into the tick.

``kind`` is one of ``proactive`` / ``feed_reply_pass``. ``slot`` is the
tick-bucket id the caller computes (a string). The ledger is purely
additive; no adapter is wired on the in-memory / no-DB path, where the
services fall back to their pre-existing in-code caps (current behaviour).
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable


SLOT_KIND_PROACTIVE = "proactive"
SLOT_KIND_FEED_REPLY = "feed_reply_pass"
#: One at-most-once claim per (character, civil day) daily goal review (CF2 /
#: COMMITMENT_LIFECYCLE_AND_FRESHNESS_PLAN §2 P2a). ``slot`` is the operator's
#: local ISO date, so the same character is reviewed at most once a day no
#: matter how many embedded ticks, distributed due-jobs and chat turns race for
#: it — the dedup is a DB unique index, never a per-process flag. Goals are
#: player-visible (PlayerGoalsPanel), so this is a visible-output claim in the
#: same sense as ``proactive``: it fences a paid LLM pass whose result the
#: player reads. NOT released on failure — a review that crashed waits for the
#: next civil day rather than re-firing on every 300s tick.
SLOT_KIND_GOAL_REVIEW = "goal_review"

#: One at-most-once claim per pending-follow-up release, keyed by the row id. The
#: distributed release path claims it just before the visible send so a reclaimed
#: job (worker crashed after sending, before marking the row resolved) finds the
#: slot taken and skips the resend. Released again on a delivery FAILURE so a
#: genuine retry can re-claim — this keeps the embedded tick's retry byte-identical.
SLOT_KIND_FOLLOW_UP = "follow_up"

#: Terminal rows prune on the same 7-day clock as the background-job queue.
DEFAULT_SLOT_RETENTION_DAYS = 7


@runtime_checkable
class VisibleSlotPort(Protocol):
    """At-most-once claim ledger for visible per-character output."""

    async def claim(
        self, character_id: str, kind: str, slot: str,
    ) -> bool:
        """Attempt to own ``(character_id, kind, slot)``.

        Returns ``True`` when this caller inserted the row (it owns the
        slot), ``False`` when the unique claim already exists (another
        executor owns it — the benign dedup path). Any integrity failure
        that is NOT the claim-uniqueness violation is a real defect and is
        re-raised, never masked as a lost claim."""
        ...

    async def release(
        self, character_id: str, kind: str, slot: str,
    ) -> bool:
        """Release a previously-claimed ``(character_id, kind, slot)``.

        Deletes the claim row so the slot can be re-claimed. Used when the work the
        claim guarded did NOT produce a visible output after all (e.g. a follow-up
        release whose delivery failed) so a legitimate retry is not suppressed by a
        stale claim. Returns ``True`` if a row was removed, ``False`` if none
        existed. Never raises for a missing row — release is idempotent."""
        ...

    async def prune(
        self, *, now: datetime, retention_days: int = DEFAULT_SLOT_RETENTION_DAYS,
    ) -> int:
        """Delete claims older than ``retention_days``; returns rows removed."""
        ...
