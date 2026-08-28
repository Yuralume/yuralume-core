"""The contract every undo step implements.

One step = one subsystem's rollback = one file under ``steps/``. The
orchestrator knows only this interface and the order in
:mod:`turn_undo.registry`; it never knows what a step does.

Two rules a step author has to keep:

* **Never raise for a condition you can foresee.** A missing repository,
  an absent snapshot, a row someone else already deleted — all of those
  are "nothing to do", so return quietly. The orchestrator does catch
  everything, but it catches so that a genuinely broken subsystem cannot
  take the other fifteen down with it, not as a substitute for handling
  your own absent dependency.
* **Write only your own tally fields.** The tally is shared and the
  result the player sees is assembled from it; a step that touches
  another's field turns a partial rollback into a false report.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

from kokoro_link.application.services.turn_undo.dependencies import (
    UndoDependencies,
)
from kokoro_link.application.services.turn_undo.result import UndoTally
from kokoro_link.domain.entities.turn_journal import TurnJournal


@dataclass(frozen=True, slots=True)
class UndoContext:
    """Everything a step is allowed to look at."""

    journal: TurnJournal
    """The pre-turn snapshot. ``turn_started_at`` is the floor for every
    time-window delete; ``turn_record_id`` may be ``None`` (a busy-defer
    turn mints none) and a step that needs it must skip, not raise."""
    deps: UndoDependencies
    now: datetime
    """One clock read for the whole rollback, so two steps stamping a
    timestamp cannot disagree about when the undo happened."""


class UndoStep(ABC):
    """One subsystem's contribution to reversing a turn."""

    name: str = "undo-step"
    """Short stable slug used in log lines when the step raises. Not a
    UI string."""

    @abstractmethod
    async def apply(self, context: UndoContext, tally: UndoTally) -> None:
        """Reverse this subsystem's share of the turn."""


__all__ = ["UndoContext", "UndoStep"]
