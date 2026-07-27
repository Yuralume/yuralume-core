"""Prometheus rendering for the P2-B shadow queue (HOSTED_CORE_SCALING §12).

Extends the internal metrics endpoint with live durable-queue series when the
shadow runtime is wired: per-status job gauges, oldest-queued age, the shadow
service counters, and coordinator lease owner/epoch. Kept in its own module so
``scheduler_metrics`` stays a pure single-writer registry and this scrape-time
renderer (which awaits ``queue.stats`` / ``lease.current`` live) has a focused
home.

Escaping mirrors ``scheduler_metrics`` (exposition format 0.0.4).
"""

from __future__ import annotations

from collections.abc import Mapping

from kokoro_link.application.services.background_shadow_coordinator import (
    ShadowCounters,
)
from kokoro_link.contracts.background_jobs import QueueStats

_JOBS = "yuralume_core_background_jobs"
_OLDEST = "yuralume_core_background_jobs_oldest_queued_age_seconds"
_SHADOW_PREFIX = "yuralume_core_shadow"
_LEASE = "yuralume_core_background_coordinator_lease"
_LEASE_EPOCH = "yuralume_core_background_coordinator_lease_epoch"

_SHADOW_COUNTER_FIELDS = (
    "enqueued",
    "deduped",
    "claimed",
    "completed",
    "skipped",
    "failed",
    "lease_lost",
)


def render_queue_metrics(
    *,
    stats: QueueStats,
    coordinator: ShadowCounters | None,
    worker: ShadowCounters | None,
    lease: tuple[str, int, object] | None,
    lease_held: bool,
) -> str:
    """Render the shadow-queue series in Prometheus text format 0.0.4.

    ``lease`` is the ``(owner_id, epoch, lease_until)`` tuple from
    ``lease.current`` (or ``None`` when the lease was never held); ``lease_held``
    is whether that lease is live at scrape time. The two service counter
    snapshots are summed field-by-field (they are disjoint by construction)."""
    lines: list[str] = []

    lines.append(f"# HELP {_JOBS} Durable background jobs by status.")
    lines.append(f"# TYPE {_JOBS} gauge")
    for status, count in sorted(stats.status_counts.items()):
        labels = _render_labels({"status": status})
        lines.append(f"{_JOBS}{labels} {int(count)}")

    lines.append(
        f"# HELP {_OLDEST} Age in seconds of the oldest ready queued job.",
    )
    lines.append(f"# TYPE {_OLDEST} gauge")
    lines.append(f"{_OLDEST} {_format_float(stats.oldest_queued_age_seconds)}")

    totals = _sum_counters(coordinator, worker)
    for field in _SHADOW_COUNTER_FIELDS:
        name = f"{_SHADOW_PREFIX}_{field}_total"
        lines.append(f"# HELP {name} Shadow runtime {field} count.")
        lines.append(f"# TYPE {name} counter")
        lines.append(f"{name} {totals[field]}")

    epoch = lease[1] if lease is not None else 0
    lines.append(
        f"# HELP {_LEASE} Coordinator lease liveness (1 = a live owner holds "
        "the lease at scrape time).",
    )
    lines.append(f"# TYPE {_LEASE} gauge")
    held_labels = _render_labels({"held": "true" if lease_held else "false"})
    lines.append(f"{_LEASE}{held_labels} {1 if lease_held else 0}")

    lines.append(
        f"# HELP {_LEASE_EPOCH} Current coordinator lease fencing epoch.",
    )
    lines.append(f"# TYPE {_LEASE_EPOCH} gauge")
    lines.append(f"{_LEASE_EPOCH} {int(epoch)}")

    return "\n".join(lines) + "\n"


def _sum_counters(
    coordinator: ShadowCounters | None, worker: ShadowCounters | None,
) -> dict[str, int]:
    totals = {field: 0 for field in _SHADOW_COUNTER_FIELDS}
    for snapshot in (coordinator, worker):
        if snapshot is None:
            continue
        for field in _SHADOW_COUNTER_FIELDS:
            totals[field] += getattr(snapshot, field)
    return totals


def _render_labels(labels: Mapping[str, str]) -> str:
    if not labels:
        return ""
    body = ",".join(
        f'{name}="{_escape_label_value(value)}"'
        for name, value in labels.items()
    )
    return "{" + body + "}"


def _escape_label_value(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
    )


def _format_float(value: float) -> str:
    return repr(float(value))
