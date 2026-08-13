"""Scheduler metrics registry.

A tiny, dependency-free Prometheus exposition source for the proactive
scheduler. It records the timing + character-count shape of the most recent
tick and renders it in Prometheus text exposition format 0.0.4.

Deliberately not ``prometheus_client``: the metric surface is small, the
scrape endpoint is internal-token protected, and pulling in a dependency for
a handful of gauges is not worth it (§12 / Phase 0 decision).

Concurrency assumption
----------------------
The scheduler runs as a **single asyncio task on one event loop**, and
``record_tick`` is only ever called from that task at the end of a tick.
Rendering happens on the same loop from the metrics route handler. Because
Python coroutines never preempt mid-statement and there is exactly one
writer, the mutable fields need **no locks**. If a second writer is ever
introduced (multiple scheduler tasks in-process), this invariant breaks and
explicit synchronisation must be added.
"""

from __future__ import annotations

import time
from collections.abc import Mapping

from kokoro_link.infrastructure.build_info import get_build_info

_TICKS_TOTAL = "yuralume_core_scheduler_ticks_total"
_TICK_FAILURES_TOTAL = "yuralume_core_scheduler_tick_failures_total"
_TICK_DURATION = "yuralume_core_scheduler_tick_duration_seconds"
_TICK_STEP_DURATION = "yuralume_core_scheduler_tick_step_duration_seconds"
_LAST_SUCCESSFUL_TICK_TIMESTAMP = (
    "yuralume_core_scheduler_last_successful_tick_timestamp_seconds"
)
_CHARACTERS = "yuralume_core_characters"
_BUILD_INFO = "yuralume_core_build_info"


class SchedulerMetrics:
    """Mutable single-writer registry for proactive-scheduler observability.

    Accounting is split so the staleness alert can actually fire:
    ``ticks_total`` counts every tick ATTEMPT (success or failure) and
    ``tick_failures_total`` counts only the failures, while the last-tick gauges
    (duration / steps / character counts) and the
    ``last_successful_tick_timestamp`` advance ONLY on a fully-successful tick.
    A run of failing ticks therefore freezes the success timestamp — which is
    exactly what a "no successful tick in N seconds" alert keys on — instead of
    a failure refreshing it with a misleading ``active=0`` snapshot."""

    def __init__(self) -> None:
        self.ticks_total: int = 0
        self.tick_failures_total: int = 0
        self._has_successful_tick: bool = False
        self.last_tick_duration_seconds: float = 0.0
        self.last_tick_step_durations: dict[str, float] = {}
        # Unix seconds (``time.time``) captured at the moment a SUCCESSFUL tick
        # is recorded — powers the last-successful-tick liveness alert (§12: a
        # stalled/failing scheduler stops advancing this even while the process
        # stays up and ``ticks_total`` keeps climbing).
        self.last_successful_tick_at: float = 0.0
        self.characters_active: int = 0
        # ``None`` = the fail-soft character-count gauge could not be read this
        # tick (repo error); rendered as *absent* rather than a misleading 0.
        self.characters_frozen: int | None = None
        self.characters_total: int | None = None

    def record_tick(
        self,
        *,
        duration_seconds: float,
        step_durations: Mapping[str, float],
        characters_active: int,
        characters_frozen: int | None = None,
        characters_total: int | None = None,
    ) -> None:
        """Record one FULLY-SUCCESSFUL tick. Counts the attempt and refreshes
        every last-successful gauge + timestamp. Called exactly once per
        successful tick, on the scheduler's own event loop (see module-level
        concurrency note)."""
        self.ticks_total += 1
        self._has_successful_tick = True
        self.last_tick_duration_seconds = duration_seconds
        # Copy so a later mutation of the caller's dict can't retroactively
        # rewrite the last-tick snapshot mid-render.
        self.last_tick_step_durations = dict(step_durations)
        self.last_successful_tick_at = time.time()
        self.characters_active = characters_active
        self.characters_frozen = characters_frozen
        self.characters_total = characters_total

    def record_tick_failure(self) -> None:
        """Record one FAILED tick attempt. Counts the attempt (``ticks_total``)
        and the failure (``tick_failures_total``) but deliberately leaves every
        last-successful gauge (duration / steps / timestamp / character counts)
        untouched, so the staleness alert on
        ``last_successful_tick_timestamp`` can still fire while ticks keep
        failing."""
        self.ticks_total += 1
        self.tick_failures_total += 1

    def render_prometheus(self) -> str:
        """Render the current state in Prometheus text format 0.0.4.

        Before the first successful tick only the two counters (``ticks_total``,
        ``tick_failures_total``) and ``build_info`` are emitted; the last-tick
        gauges are omitted entirely (an absent series is honest, a ``0`` duration
        would look like a real instant tick).
        """
        lines: list[str] = []

        lines.append(
            f"# HELP {_TICKS_TOTAL} Total proactive scheduler tick attempts "
            "(successful and failed).",
        )
        lines.append(f"# TYPE {_TICKS_TOTAL} counter")
        lines.append(f"{_TICKS_TOTAL} {self.ticks_total}")

        lines.append(
            f"# HELP {_TICK_FAILURES_TOTAL} Proactive scheduler tick attempts "
            "that failed before completing.",
        )
        lines.append(f"# TYPE {_TICK_FAILURES_TOTAL} counter")
        lines.append(f"{_TICK_FAILURES_TOTAL} {self.tick_failures_total}")

        build = get_build_info()
        labels = _render_labels(
            {"version": build.version, "sha": build.build.commit_sha or ""},
        )
        lines.append(f"# HELP {_BUILD_INFO} Build metadata (constant 1).")
        lines.append(f"# TYPE {_BUILD_INFO} gauge")
        lines.append(f"{_BUILD_INFO}{labels} 1")

        if self._has_successful_tick:
            lines.extend(self._render_last_tick())

        return "\n".join(lines) + "\n"

    def _render_last_tick(self) -> list[str]:
        lines: list[str] = []

        lines.append(
            f"# HELP {_TICK_DURATION} Wall-clock duration of the most recent "
            "scheduler tick.",
        )
        lines.append(f"# TYPE {_TICK_DURATION} gauge")
        lines.append(
            f"{_TICK_DURATION} {_format_float(self.last_tick_duration_seconds)}",
        )

        lines.append(
            f"# HELP {_TICK_STEP_DURATION} Accumulated duration of each step "
            "in the most recent tick.",
        )
        lines.append(f"# TYPE {_TICK_STEP_DURATION} gauge")
        for step, seconds in self.last_tick_step_durations.items():
            labels = _render_labels({"step": step})
            lines.append(
                f"{_TICK_STEP_DURATION}{labels} {_format_float(seconds)}",
            )

        lines.append(
            f"# HELP {_LAST_SUCCESSFUL_TICK_TIMESTAMP} Unix time of the most "
            "recent fully-successful tick (frozen while ticks fail).",
        )
        lines.append(f"# TYPE {_LAST_SUCCESSFUL_TICK_TIMESTAMP} gauge")
        lines.append(
            f"{_LAST_SUCCESSFUL_TICK_TIMESTAMP} "
            f"{_format_float(self.last_successful_tick_at)}",
        )

        lines.append(f"# HELP {_CHARACTERS} Character counts by state.")
        lines.append(f"# TYPE {_CHARACTERS} gauge")
        lines.append(
            f"{_CHARACTERS}{_render_labels({'state': 'active'})} "
            f"{self.characters_active}",
        )
        if self.characters_frozen is not None:
            lines.append(
                f"{_CHARACTERS}{_render_labels({'state': 'frozen'})} "
                f"{self.characters_frozen}",
            )
        if self.characters_total is not None:
            lines.append(
                f"{_CHARACTERS}{_render_labels({'state': 'total'})} "
                f"{self.characters_total}",
            )

        return lines


def _render_labels(labels: Mapping[str, str]) -> str:
    if not labels:
        return ""
    body = ",".join(
        f'{name}="{_escape_label_value(value)}"'
        for name, value in labels.items()
    )
    return "{" + body + "}"


def _escape_label_value(value: str) -> str:
    """Escape a Prometheus label value: backslash, double-quote, newline
    (exposition format 0.0.4)."""
    return (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
    )


def _format_float(value: float) -> str:
    # ``repr`` keeps full float precision without scientific-notation surprises
    # for the magnitudes we emit (sub-second durations, unix timestamps).
    return repr(float(value))
