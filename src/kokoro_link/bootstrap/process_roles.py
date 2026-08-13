"""Process-role → component matrix.

The matrix is the single source of truth for *what a role starts*. ``app.py``
gates router registration and lifespan ``.start()`` calls off these flags;
``container.py`` reads ``enable_realtime_outbox_writer`` to decide whether to
wrap the in-process event buses, and ``run_background_coordinator`` /
``run_background_worker`` to decide which halves of the distributed execution
machinery to build. Gating lives at those seams — never at container
construction — so the container shape stays identical across roles and the test
blast radius is minimal.

§2.1 dedicated roles: the ``coordinator`` / ``worker`` / ``connector`` processes
each start one slice and nothing else. ``coordinator`` runs the leader-lease +
due-discovery loop and the lease-gated site-global world-event scheduler;
``worker`` runs the queue
claim/execute loop (+ realtime outbox writer); ``connector`` runs only the
messaging connectors. None of the three serve public API routes — a hard
security red line (§11): the only surface they expose is the loopback
``/health`` + internal metrics scrape.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ComponentMatrix:
    """Booleans describing which subsystems a process role turns on.

    ``serve_metrics_route`` gates the Phase 0 internal metrics endpoint (loopback
    scrape). ``start_schedulers`` is the *embedded* in-process scheduler pair
    (proactive + world-event); ``start_world_event_scheduler`` additionally
    assigns only the world-event singleton to a dedicated coordinator — distinct
    from the distributed
    ``run_background_coordinator`` / ``run_background_worker`` loops, which are
    the §2.1 dedicated roles' coordinator/worker tasks and can run in a process
    that starts no embedded scheduler at all. ``enable_realtime_outbox_writer``
    wraps the two event buses with the durable-outbox decorators on any role that
    executes ticks but serves no SSE (background / worker).
    """

    serve_api_routes: bool
    serve_cloud_internal_routes: bool
    serve_metrics_route: bool
    start_schedulers: bool
    start_connectors: bool
    run_studio_recovery: bool
    run_background_coordinator: bool
    run_background_worker: bool
    enable_realtime_outbox_writer: bool

    @property
    def start_world_event_scheduler(self) -> bool:
        """Whether this role owns the site-global RSS/curation loop.

        Embedded roles keep their existing scheduler shape. In the dedicated
        topology the coordinator is the sole eligible owner; its replicas are
        fenced by the existing coordinator lease.
        """
        return self.start_schedulers or (
            self.run_background_coordinator
            and not self.run_background_worker
        )

    @property
    def requires_cloud_provider_credentials(self) -> bool:
        """Whether this role can execute a Cloud Gateway-backed capability."""

        return (
            self.serve_api_routes
            or self.start_schedulers
            or self.start_connectors
            or self.run_background_worker
        )


# Studio recovery stays with any role that serves API routes: Studio jobs
# execute as asyncio tasks *inside the API process* (process-local locks), so
# recovery must run wherever the executor lives — never in a headless role.
#
# ``all`` / ``background`` keep BOTH the embedded scheduler AND the durable
# coordinator/worker (the transitional Phase 1/2 shape: they ride together when
# the postgres backend / shadow is on). The dedicated §2.1 roles split those
# apart: ``coordinator`` runs its loop plus the lease-gated global world-event
# scheduler, ``worker`` only the worker loop, ``connector`` only the connectors.
_MATRIX: dict[str, ComponentMatrix] = {
    "all": ComponentMatrix(
        serve_api_routes=True,
        serve_cloud_internal_routes=True,
        serve_metrics_route=True,
        start_schedulers=True,
        start_connectors=True,
        run_studio_recovery=True,
        run_background_coordinator=True,
        run_background_worker=True,
        enable_realtime_outbox_writer=False,
    ),
    "api": ComponentMatrix(
        serve_api_routes=True,
        serve_cloud_internal_routes=True,
        serve_metrics_route=True,
        start_schedulers=False,
        start_connectors=False,
        run_studio_recovery=True,
        run_background_coordinator=False,
        run_background_worker=False,
        enable_realtime_outbox_writer=False,
    ),
    "background": ComponentMatrix(
        serve_api_routes=False,
        serve_cloud_internal_routes=False,
        serve_metrics_route=True,
        start_schedulers=True,
        start_connectors=True,
        run_studio_recovery=False,
        run_background_coordinator=True,
        run_background_worker=True,
        enable_realtime_outbox_writer=True,
    ),
    # §2.1 dedicated coordinator: leader lease + due discovery + global
    # social_tick enqueue + reconcile, obeying the three-state execution-mode
    # barrier, plus the lease-gated site-global world-event loop. No proactive
    # scheduler, worker execution, connectors, Studio recovery, or public API.
    # It never publishes
    # proactive/feed events → no realtime outbox writer.
    "coordinator": ComponentMatrix(
        serve_api_routes=False,
        serve_cloud_internal_routes=False,
        serve_metrics_route=True,
        start_schedulers=False,
        start_connectors=False,
        run_studio_recovery=False,
        run_background_coordinator=True,
        run_background_worker=False,
        enable_realtime_outbox_writer=False,
    ),
    # §2.1 dedicated worker: queue claim + job handlers (character_tick /
    # social_tick / warmup execution). It executes ticks that publish
    # proactive/feed events, so it IS the realtime outbox writer (the api reader
    # tails the outbox for SSE). No embedded scheduler, no connectors, no
    # coordinator, no public API, no Studio recovery.
    "worker": ComponentMatrix(
        serve_api_routes=False,
        serve_cloud_internal_routes=False,
        serve_metrics_route=True,
        start_schedulers=False,
        start_connectors=False,
        run_studio_recovery=False,
        run_background_coordinator=False,
        run_background_worker=True,
        enable_realtime_outbox_writer=True,
    ),
    # §2.1 dedicated connector: Telegram/Discord/WhatsApp connectors only
    # (per-account TTL lease). No queue coordinator/worker, no embedded
    # scheduler, no public API, no Studio recovery, no realtime component.
    "connector": ComponentMatrix(
        serve_api_routes=False,
        serve_cloud_internal_routes=False,
        serve_metrics_route=True,
        start_schedulers=False,
        start_connectors=True,
        run_studio_recovery=False,
        run_background_coordinator=False,
        run_background_worker=False,
        enable_realtime_outbox_writer=False,
    ),
}


def matrix_for_role(role: str) -> ComponentMatrix:
    """Return the component matrix for an implemented process role.

    Unknown roles are rejected upstream at settings load, so anything not in the
    table here is a programming error and raises rather than silently falling
    back.
    """

    try:
        return _MATRIX[role]
    except KeyError:
        raise ValueError(
            f"no component matrix for process role {role!r}; "
            f"expected one of {', '.join(sorted(_MATRIX))}",
        ) from None
