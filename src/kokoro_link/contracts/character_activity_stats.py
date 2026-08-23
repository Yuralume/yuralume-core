"""Cross-tenant character activity counts — the hosted operator's load gauge.

The Cloud admin console's dashboard asks one question this port answers:
**how many characters exist, and how many of them still cost the cluster
background work?** Everything else about a character (who owns it, what it
said) is out of scope here; this is an aggregate read model, never a listing.

Why it is not a method on ``CharacterRepositoryPort``: that port returns
*entities*, and every existing "how many" answer there is a ``len()`` over a
fully deserialised list. A dashboard poll must not deserialise every character
row in the deployment to print two integers, so the counting lives in SQL and
gets its own narrow port.

The two counts are deliberately asymmetric:

* ``total`` is every stored character — the shelf, including the frozen and
  the long-abandoned.
* ``schedulable`` is the *state* half of "still costs us something": not
  frozen, not subscription-locked. It mirrors
  :meth:`CharacterRepositoryPort.list_active`'s filter exactly, because that
  is the set the reconciler walks.

The *dormancy* half (NF4) cannot live here: the window is a per-tier
control-plane knob, not a column, so the caller resolves it per tier and asks
:meth:`count_engaged_since` for the survivors. That is why the buckets are
grouped by tier at all — one cutoff per tier, computed by whoever knows the
tier→knob mapping.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True, slots=True)
class TierCharacterCounts:
    """One tier's slice of the character table."""

    tier: str
    total: int
    schedulable: int
    """Not frozen and not subscription-locked — the reconciler's working set
    before the dormancy question is asked."""


class CharacterActivityStatsPort(Protocol):
    async def counts_by_tier(self) -> list[TierCharacterCounts]:
        """Every character, bucketed by its owner's Cloud tenant tier.

        Characters whose owner row is missing or carries no tier still appear,
        under the schema's default tier — a count that silently drops rows is
        worse than one bucketed slightly wrong, because the dashboard's whole
        job is "is this number bigger than I thought"."""

    async def count_engaged_since(self, tier: str, cutoff: datetime) -> int:
        """Schedulable characters in ``tier`` whose owner engaged at/after ``cutoff``.

        "Engaged" is ``CharacterState.last_active_at`` — the single
        foreground-interaction anchor (see
        :mod:`kokoro_link.application.services.character_activity_anchor`).
        A character that has *never* been interacted with is NOT counted:
        ``last_active_at IS NULL`` is dormancy's answer to "has the player ever
        engaged this character", exactly as
        :func:`~kokoro_link.application.services.due_job_scheduler._default_dormancy_resolver`
        reads it. Borrowing ``created_at`` here (as the idle down-shift does)
        would report a never-touched character as active."""
