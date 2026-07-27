"""Pair-lease port for encounter-style background jobs (Phase 5 §6.1).

An *encounter* (and any future two-character background mutation) runs on a
PAIR of characters. The existing per-character single-flight guard protects
one character at a time; it cannot express "the same encounter must hold BOTH
characters, and neither character may be simultaneously claimed by a second,
different pair". This port is that guard.

Design invariants every adapter (in-memory + SQLAlchemy) MUST honour
identically so the parity tests hold:

* **Canonical pair** — ``acquire(char_a, char_b, ...)`` sorts the two ids to a
  canonical ``(low, high)`` order, so ``(A, B)`` and ``(B, A)`` address the
  same logical pair and the same two underlying per-character lease rows.

* **Atomic two-row acquire, all-or-nothing** — a pair lease is two
  per-character rows (``encounter:<char_id>``). ``acquire`` takes BOTH in one
  transaction, always in ascending row order, and is atomic: if EITHER row is
  already held by a live lease belonging to another holder, the whole acquire
  fails and leaves NO residual row behind (the transaction rolls back). It
  never returns a half-held pair.

* **Single character ⇒ single pair** — because the unit of leasing is the
  *character* row and every acquisition stamps a fresh unique ``token`` as the
  holder identity, a character already leased (by ANY pair, even one driven by
  the same worker) blocks a second acquire touching it while the lease is live.
  This is what forbids "one character in two concurrent encounters".

* **Deadlock-free** — every acquire locks its (up to two) rows in ascending
  ``encounter:<char_id>`` name order. With a single global lock order no
  wait-for cycle can form, so crossing pairs ``(A,B)`` and ``(B,C)`` cannot
  deadlock: whichever transaction takes the shared row ``encounter:B`` first
  wins it, the other blocks and then fails that row (B is held) — B is never
  claimed by both.

* **TTL + heartbeat, no long transaction** — the lease carries a TTL
  (``lease_until``); the holder renews it with ``heartbeat`` between LLM calls
  rather than holding a DB transaction open across a model round-trip. An
  expired lease is re-acquirable by any holder and can no longer be renewed by
  its former holder (tenure/fencing semantics identical to the background
  coordinator lease). ``heartbeat`` returns ``False`` the instant the lease was
  lost (expired, or the row taken over) — the caller's abort cue.

No epoch is exposed: the fencing identity is the opaque per-acquisition
``token``. Once another holder takes over a row (bumping its epoch under the
hood), the stale holder's ``token`` no longer matches and its ``heartbeat``
fails — the same abort signal the studio lease derives from an epoch mismatch.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable


LEASE_NAME_PREFIX = "encounter:"
"""Well-known ``background_runtime_leases.name`` namespace for the per-character
half of an encounter pair lease. Distinct from ``background-coordinator`` /
``studio:<id>`` so pair leases never collide with those single-name leases."""


def pair_lease_name(character_id: str) -> str:
    """Row name for one character's half of a pair lease."""
    return f"{LEASE_NAME_PREFIX}{character_id}"


def canonical_pair(a: str, b: str) -> tuple[str, str]:
    """Sort two character ids into the canonical ``(low, high)`` pair.

    Mirrors ``character_relationship_service.canonical_pair`` so a pair lease
    and its relationship row address the two characters in the same order.
    Raises ``ValueError`` on an empty id or a self-pair (a character cannot
    have an encounter with itself).
    """
    first = a.strip()
    second = b.strip()
    if not first or not second:
        raise ValueError("pair-lease character ids must be non-empty")
    if first == second:
        raise ValueError("pair-lease cannot pair a character with itself")
    return tuple(sorted((first, second)))  # type: ignore[return-value]


@dataclass(frozen=True, slots=True)
class PairLeaseHandle:
    """Opaque proof of a held pair lease, returned by ``acquire``.

    ``char_a`` / ``char_b`` are canonical-sorted (``char_a < char_b``).
    ``token`` is the unique per-acquisition holder identity used to fence
    ``heartbeat`` / ``release`` against a takeover. ``lease_until`` is the
    current expiry; ``ttl_seconds`` is reused by ``heartbeat`` to push it out.
    """

    char_a: str
    char_b: str
    owner: str
    token: str
    ttl_seconds: int
    lease_until: datetime

    @property
    def names(self) -> tuple[str, str]:
        """The two underlying row names, in canonical (ascending) order."""
        return (pair_lease_name(self.char_a), pair_lease_name(self.char_b))


@runtime_checkable
class PairLeasePort(Protocol):
    """Atomic two-character TTL lease for encounter-style pair jobs."""

    async def acquire(
        self,
        char_a: str,
        char_b: str,
        owner: str,
        *,
        ttl_seconds: int,
        now: datetime,
    ) -> PairLeaseHandle | None:
        """Atomically lease BOTH characters (canonical-sorted) for ``owner``.

        Returns a :class:`PairLeaseHandle` on success, or ``None`` when either
        character is already held by a live lease belonging to a different
        holder — in which case NO row is created or taken over (all-or-nothing,
        no residual). ``owner`` is informational (which worker holds it); the
        fencing identity is a fresh token minted inside. ``now`` is the caller's
        clock, used for both the liveness check and the persisted expiry so the
        decision and the stored instant share one clock.
        """
        ...

    async def heartbeat(
        self, handle: PairLeaseHandle, *, now: datetime,
    ) -> bool:
        """Renew BOTH rows of ``handle``, pushing expiry to
        ``now + handle.ttl_seconds``.

        ``True`` on success. ``False`` — the lease is LOST — when either row is
        no longer held by this handle's ``token`` OR has already expired at
        ``now`` (an expired lease is not renewable by its former holder; it must
        be re-acquired). On a lost lease no row is renewed (the still-held row,
        if any, keeps its old expiry and lapses on its own TTL). The caller MUST
        abort its pair job on ``False``.
        """
        ...

    async def release(self, handle: PairLeaseHandle) -> None:
        """Vacate both rows this handle still owns. A row already taken over by
        another holder (``token`` mismatch) is left untouched — releasing under
        a superseded token must never disturb the new owner. Idempotent."""
        ...
