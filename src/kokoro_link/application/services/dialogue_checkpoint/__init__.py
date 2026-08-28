"""Cumulative dialogue checkpoint (DH3).

The package is split by *when* each piece runs, because that is the
distinction the design rests on:

``window``
    pure geometry — where the covered / middle / raw-tail boundaries
    are, and what fits a token budget. Shared, so the reader and the
    updater cannot disagree about them.
``reader``
    request path. Reads a checkpoint, never writes one.
``updater``
    background post-turn. Writes at most one checkpoint per run, and
    only when a merge earned it.

Everything here is inert while ``FEATURE_DIALOGUE_CHECKPOINT``
(``KOKORO_DIALOGUE_CHECKPOINT_ENABLED``) is off — the chat service does
not construct either half.
"""

from __future__ import annotations

from kokoro_link.application.services.dialogue_checkpoint.reader import (
    DialogueCheckpointReader,
    DialoguePromptContext,
)
from kokoro_link.application.services.dialogue_checkpoint.updater import (
    STUCK_STREAK_WARN_AT,
    CheckpointUpdateOutcome,
    CheckpointUpdateReport,
    DialogueCheckpointUpdater,
)
from kokoro_link.application.services.dialogue_checkpoint.window import (
    PROMPT_RAW_TAIL_MESSAGES,
    WINDOW_PRESSURE_SAFETY_MARGIN,
    DialogueWindow,
    fit_to_budget,
    split_window,
    total_tokens,
    window_pressure_threshold,
)

__all__ = [
    "PROMPT_RAW_TAIL_MESSAGES",
    "STUCK_STREAK_WARN_AT",
    "WINDOW_PRESSURE_SAFETY_MARGIN",
    "CheckpointUpdateOutcome",
    "CheckpointUpdateReport",
    "DialogueCheckpointReader",
    "DialogueCheckpointUpdater",
    "DialogueWindow",
    "DialoguePromptContext",
    "fit_to_budget",
    "split_window",
    "total_tokens",
    "window_pressure_threshold",
]
