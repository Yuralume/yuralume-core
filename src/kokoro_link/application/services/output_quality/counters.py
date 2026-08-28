"""Process-lifetime totals for the QG output-quality gate.

Two things are counted here, and only the second is an alarm.

**Outcomes, per surface.** One integer per ``(surface, outcome)`` pair.
Labelled rather than one field per combination because the outcome set is
fixed by the orchestrator while the surface set grows with every seam that
adopts it — a dataclass field per pair would need editing in this file
every time a wave-3 ticket lands, which is exactly the kind of bookkeeping
that gets forgotten and turns a new surface into an invisible one.

**Consecutive fail-open runs, per surface.** The gate fails *open*: a judge
that cannot answer lets the candidate through unreviewed. That is the right
direction (a broken judge must not stop the character talking) and also the
quietest possible failure — nothing breaks, the quality bar simply stops
existing. So consecutive ``gate_error_failopen`` outcomes are counted per
surface and crossing :data:`DEFAULT_FAILOPEN_ALARM_STREAK` raises
``failopen_outage`` once per crossing, mirroring
:class:`~kokoro_link.application.services.outcome_claim_guard.OutcomeClaimGuard`'s
``judge_outage``. Any other outcome on that surface resets its streak: one
timeout is weather, three in a row is an outage.

Single-process asyncio, so plain ``int`` — no locks, no atomics.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping

_LOGGER = logging.getLogger(__name__)

DEFAULT_FAILOPEN_ALARM_STREAK = 3
"""Consecutive fail-open reviews on one surface before the outage alarm.

Three, matching ``DEFAULT_FAILURE_ALARM_STREAK`` on the honesty guard, and
for the same reason: a single provider timeout costs exactly one unreviewed
message, while three in a row is the shape of a route that is actually
down."""


class OutputQualityCounters:
    """In-memory ``(surface, outcome)`` totals plus the fail-open alarm.

    Deliberately *not* a dataclass with one field per counter (the shape
    :mod:`outcome_claim_metrics` reflects over): the surface half of the
    key is open-ended, so the renderer emits Prometheus **labels** off
    this mapping instead of enumerating fields.
    """

    __slots__ = ("_alarm_streak", "_failopen_outages", "_streaks", "_totals")

    def __init__(self, *, failopen_alarm_streak: int = DEFAULT_FAILOPEN_ALARM_STREAK) -> None:
        self._totals: dict[tuple[str, str], int] = {}
        self._streaks: dict[str, int] = {}
        self._failopen_outages: dict[str, int] = {}
        self._alarm_streak = max(1, int(failopen_alarm_streak))

    # -- write ------------------------------------------------------------

    def record(self, surface: str, outcome: str) -> None:
        """Count one disposal.

        Called by the orchestrator for every review it completes, and by
        callers directly for surface-specific dispositions the orchestrator
        cannot see (feed's ``hard_degraded`` — the post shipped, minus the
        broken tool prompt). Never raises: a counter must not be able to
        kill the turn it is describing.
        """
        key = ((surface or "unknown").strip() or "unknown",
               (outcome or "unknown").strip() or "unknown")
        self._totals[key] = self._totals.get(key, 0) + 1
        self._track_failopen_streak(key[0], key[1])

    # -- read (the metrics renderer's whole surface) ----------------------

    def snapshot(self) -> Mapping[tuple[str, str], int]:
        """Copy of the ``(surface, outcome) -> total`` map."""
        return dict(self._totals)

    def failopen_outages(self) -> Mapping[str, int]:
        """Copy of the ``surface -> outage crossings`` map."""
        return dict(self._failopen_outages)

    def total(self, surface: str, outcome: str) -> int:
        return self._totals.get((surface, outcome), 0)

    def failopen_streak(self, surface: str) -> int:
        """Current unbroken run of fail-open reviews on *surface*."""
        return self._streaks.get(surface, 0)

    # -- internals --------------------------------------------------------

    def _track_failopen_streak(self, surface: str, outcome: str) -> None:
        # Imported lazily: orchestrator imports this module, so a module
        # level import back would close the cycle.
        from kokoro_link.application.services.output_quality.orchestrator import (
            OUTCOME_GATE_ERROR_FAILOPEN,
        )

        if outcome != OUTCOME_GATE_ERROR_FAILOPEN:
            self._streaks.pop(surface, None)
            return
        streak = self._streaks.get(surface, 0) + 1
        self._streaks[surface] = streak
        if streak != self._alarm_streak:
            # Below the threshold the per-call warning the orchestrator
            # already logged is enough; above it, the crossing has been
            # announced once and repeating it every tick buries it.
            return
        self._failopen_outages[surface] = self._failopen_outages.get(surface, 0) + 1
        _LOGGER.error(
            "output quality: %d consecutive fail-open reviews on surface=%s — "
            "player-visible output on this surface is currently shipping "
            "UNREVIEWED. Suspect the novelty_gate feature route, not the "
            "composers.",
            streak, surface,
        )


__all__ = ["DEFAULT_FAILOPEN_ALARM_STREAK", "OutputQualityCounters"]
