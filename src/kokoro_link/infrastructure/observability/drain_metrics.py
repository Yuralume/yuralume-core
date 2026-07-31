"""Prometheus rendering for the rolling-deploy drain surface (GD1-A).

``HOSTED_PROMPT_PACK_DISTRIBUTION_PLAN`` §7.3 GD1 step 3 makes these two series
load-bearing rather than merely informative: after the router stops sending the
replica traffic and the drain request lands, ``deploy.sh`` polls
``yuralume_core_active_turns`` down to zero (capped at 180s, §7.2-2) before it
recreates the container. The gauge *is* the gate.

``yuralume_core_draining`` is the acknowledgement half of that handshake. Without
it, an ``active_turns 0`` reading is ambiguous — a replica that never received
the drain request looks exactly like one that received it and finished its work,
and the first will keep taking new turns right up to the SIGKILL.

Rendered unconditionally, hosted and self-host alike: the scrape is token-gated
and loopback-only, and a permanent ``draining 0`` costs two lines. Mirrors
``execution_mode_metrics`` / ``prompt_pack_metrics`` — no ``prometheus_client``,
text exposition format 0.0.4, and the values arrive as arguments so this module
never learns how they are reached from the container.
"""

from __future__ import annotations

_ACTIVE_TURNS = "yuralume_core_active_turns"
_DRAINING = "yuralume_core_draining"


def render_drain_metrics(*, draining: bool, active_turns: int) -> str:
    """Render the drain series in Prometheus text format 0.0.4."""
    lines: list[str] = []

    lines.append(
        f"# HELP {_ACTIVE_TURNS} Player chat turns in flight on this replica. "
        "A rolling deploy waits for this to reach 0 before recreating the "
        "container.",
    )
    lines.append(f"# TYPE {_ACTIVE_TURNS} gauge")
    lines.append(f"{_ACTIVE_TURNS} {int(active_turns)}")

    lines.append(
        f"# HELP {_DRAINING} 1 once this replica has been asked to drain "
        "(new turns refused, SSE streams closed). Never returns to 0 without a "
        "restart.",
    )
    lines.append(f"# TYPE {_DRAINING} gauge")
    lines.append(f"{_DRAINING} {1 if draining else 0}")

    return "\n".join(lines) + "\n"
