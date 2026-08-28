"""Turn undo — one file per subsystem the rollback touches.

``TurnUndoService`` (one module up) reads the journal, builds an
:class:`~.step.UndoContext`, and runs :data:`~.registry.UNDO_STEPS`
against it. It knows nothing about what any step does; a step knows
nothing about the ones around it. Everything shared between them is
here:

* :mod:`.dependencies` — the repository bundle every step reads from
* :mod:`.result` — the tally steps write into and the DTO built from it
* :mod:`.step` — the interface and the per-undo context
* :mod:`.registry` — the order, with the reasoning for it
* :mod:`.steps` — the implementations
"""

from __future__ import annotations

from kokoro_link.application.services.turn_undo.dependencies import (
    UndoDependencies,
)
from kokoro_link.application.services.turn_undo.registry import UNDO_STEPS
from kokoro_link.application.services.turn_undo.result import (
    UndoResult, UndoTally,
)
from kokoro_link.application.services.turn_undo.step import (
    UndoContext, UndoStep,
)

__all__ = [
    "UNDO_STEPS",
    "UndoContext",
    "UndoDependencies",
    "UndoResult",
    "UndoStep",
    "UndoTally",
]
