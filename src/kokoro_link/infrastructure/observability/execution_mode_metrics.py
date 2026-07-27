"""Prometheus rendering for the execution-ownership gate (P3-B, §12 / §15).

Extends the internal metrics endpoint with the current background execution
mode + epoch and the scheduler's ownership-skip counter, but ONLY when the
runtime-ownership port is wired (hosted opt-in). Self-host leaves the port
unwired, so these series never appear — matching the "zero new surface unless
enforced" red line.

Escaping / exposition format mirrors ``scheduler_metrics`` (0.0.4).
"""

from __future__ import annotations

from collections.abc import Mapping

_MODE = "yuralume_core_background_execution_mode"
_EPOCH = "yuralume_core_background_execution_mode_epoch"
_SKIPS = "yuralume_core_background_ownership_skips_total"


def render_execution_mode_metrics(
    *, mode: str, epoch: int, ownership_skips_total: int,
) -> str:
    """Render the execution-mode series in Prometheus text format 0.0.4.

    ``mode`` is a ``{mode="..."} 1`` info-style gauge (exactly one line, the
    live mode), ``epoch`` is the current CAS epoch gauge, and
    ``ownership_skips_total`` is the scheduler's fail-closed skip counter."""
    lines: list[str] = []

    lines.append(
        f"# HELP {_MODE} Current background execution mode (1 = active mode).",
    )
    lines.append(f"# TYPE {_MODE} gauge")
    labels = _render_labels({"mode": mode})
    lines.append(f"{_MODE}{labels} 1")

    lines.append(f"# HELP {_EPOCH} Current execution-mode CAS epoch.")
    lines.append(f"# TYPE {_EPOCH} gauge")
    lines.append(f"{_EPOCH} {int(epoch)}")

    lines.append(
        f"# HELP {_SKIPS} Ticks/dispatches skipped because ownership did not "
        "confirm embedded mode.",
    )
    lines.append(f"# TYPE {_SKIPS} counter")
    lines.append(f"{_SKIPS} {int(ownership_skips_total)}")

    return "\n".join(lines) + "\n"


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
