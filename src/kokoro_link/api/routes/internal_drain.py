"""Internal drain endpoint for rolling deployments (GD1-A).

``POST /api/internal/v1/drain`` — the request ``deploy.sh`` sends to a single api
replica's loopback management port after the router has already removed it from
the upstream pool (``HOSTED_PROMPT_PACK_DISTRIBUTION_PLAN`` §7.3 GD1 step 2).
It flips this process's :class:`DrainState`, which closes the long-lived SSE
streams gently and refuses new turns; the deploy then polls
``yuralume_core_active_turns`` on the metrics scrape to zero before recreating
the container.

Auth is deliberately the **same channel** as the internal metrics scrape rather
than a second token: both are the loopback management surface of one replica,
both are reached over the same port by the same deploy script, and a second
credential would be one more thing to rotate for exactly zero isolation gain
(anyone able to drain a replica can already read its scrape). It inherits the
metrics channel's fail-closed posture unchanged — 503 when the channel is
unconfigured, 401 on a bad or missing token.

Not part of the public API surface: mounted under ``/api/internal/v1`` beside
the metrics route, off the ``/api/v1`` operator surface, and never behind the
operator JWT. Self-host mounts it too — as it already mounts the metrics route —
and with no metrics token configured every call answers 503, so the self-host
behaviour is unchanged whether or not anything ever posts here.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from kokoro_link.api.dependencies import get_container
from kokoro_link.api.routes.internal_metrics import require_metrics_token
from kokoro_link.bootstrap.container import ServiceContainer

_LOGGER = logging.getLogger(__name__)

router = APIRouter(tags=["internal-drain"])


class DrainResponse(BaseModel):
    """What the deploy script learns from one drain request."""

    draining: bool = Field(
        description="Always true once the request has been accepted.",
    )
    active_turns: int = Field(
        description=(
            "Player turns still in flight on this replica at the moment the "
            "drain was applied. The deploy waits for the metrics gauge of the "
            "same name to reach 0; this is the first reading of it."
        ),
    )


@router.post(
    "/drain",
    dependencies=[Depends(require_metrics_token)],
    response_model=DrainResponse,
)
async def begin_drain(
    container: ServiceContainer = Depends(get_container),
) -> DrainResponse:
    """Ask this replica to stop taking new work.

    Idempotent: drain is a one-way flip, so a retry (or a deploy that got half
    way through a roll and started over) answers 200 with the current turn count
    rather than erroring. The response body is the whole point of the retry —
    the caller wants to know how much is still running.
    """
    state = getattr(container, "drain_state", None)
    if state is None:
        # Only reachable on a hand-built container that skipped the field. Fail
        # loud: a deploy that silently believed it had drained a replica would
        # kill live turns believing they were finished.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="drain state not wired",
        )
    already = state.draining
    state.begin_drain()
    if not already:
        _LOGGER.warning(
            "drain requested: refusing new turns, %d still in flight",
            state.active_turns,
        )
    return DrainResponse(draining=True, active_turns=state.active_turns)
