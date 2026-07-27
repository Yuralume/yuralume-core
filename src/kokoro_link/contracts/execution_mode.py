"""Runtime execution-ownership port (HOSTED_CORE_SCALING §2.2 / §13 Phase 3).

The single source of truth for whether background work runs in the
``embedded`` (self-host default, in-process scheduler) or ``distributed``
(hosted coordinator/worker) mode. It exists to make the two modes MUTUALLY
EXCLUSIVE at the database level (§15): a mode flip is a monotonic-epoch CAS,
and BOTH sides fail closed against it so dual execution is impossible.

Why a dedicated table rather than reusing ``background_runtime_leases``: the
lease port's TTL-CAS ``acquire`` semantics let ordinary lease traffic steal a
non-renewed row once it expires — exactly the wrong behaviour for a mode flag
that must stay put until an operator (or the rollback CLI) explicitly flips it.
§2.2 lists ``RuntimeOwnershipPort`` separately from the lease port for this
reason.

Absent-row semantics (self-host red line): a deployment that never wrote the
row reads ``("embedded", 0)`` — the historical single-container behaviour. No
seed row is required; the first ``flip`` from epoch 0 INSERTs it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable


EXECUTION_MODE_ROW_NAME = "default"
"""Single-row primary key. The table holds exactly one logical mode row."""

MODE_EMBEDDED = "embedded"
MODE_PAUSED = "paused"
MODE_DISTRIBUTED = "distributed"

VALID_MODES = frozenset({MODE_EMBEDDED, MODE_PAUSED, MODE_DISTRIBUTED})


def transition_allowed(current_mode: str, target_mode: str) -> bool:
    """Return whether an operator may make this ownership transition.

    Direct embedded↔distributed changes are forbidden: PAUSED is the fencing
    barrier that stops new work before the other owner is enabled.
    """
    if current_mode == target_mode:
        return True
    return {current_mode, target_mode} != {MODE_EMBEDDED, MODE_DISTRIBUTED}

# The value an absent row resolves to (self-host default): embedded at epoch 0.
ABSENT_MODE = MODE_EMBEDDED
ABSENT_EPOCH = 0


@runtime_checkable
class RuntimeOwnershipPort(Protocol):
    """Reads and CAS-flips the single background execution-mode row.

    Both adapters (SQLAlchemy + in-memory twin) MUST honour these semantics
    identically so the parity tests hold:

    * ``get`` — return ``(mode, epoch)``; an absent row resolves to
      ``("embedded", 0)`` (never raises for absence).
    * ``flip`` — compare-and-swap: succeed ONLY when the current epoch equals
      ``expected_epoch`` (an absent row counts as epoch 0 and the flip INSERTs
      the row), writing ``target_mode`` with ``epoch + 1`` and returning the new
      epoch. On any mismatch return ``None`` and change nothing. Concurrent
      flips against the same epoch: exactly one wins, the rest get ``None``.
    """

    async def get(self) -> tuple[str, int]:
        """``(mode, epoch)``; absent row → ``("embedded", 0)``."""
        ...

    async def flip(
        self, target_mode: str, expected_epoch: int, *, now: datetime,
    ) -> int | None:
        """CAS the mode. Returns the new epoch (``expected_epoch + 1``) on
        success, or ``None`` when the current epoch != ``expected_epoch``.
        An absent row is epoch 0: flipping from 0 INSERTs the row."""
        ...
