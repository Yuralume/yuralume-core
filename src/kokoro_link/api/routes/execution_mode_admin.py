"""Admin control of the background execution mode (HOSTED_CORE_SCALING §11/§15).

The operator-facing surface for the mode flip that makes embedded / distributed
execution mutually exclusive. Admin-gated (router-level ``require_admin``, same
as ``background_jobs_admin``) with a dedicated audit channel: EVERY flip attempt
is logged with the admin identity and the from→to mode + epochs, including the
failures (a 409 CAS mismatch or a 503 unwired), so the trail records the try,
not only the successes.

Mounted under ``/api/v1`` only when the ownership port could be wired
(``background_shadow=postgres`` OR ``background_backend=postgres``); otherwise
the paths 404/405 exactly like the pre-P3-B surface. When registered but the
port is unwired in this process the routes return 503 (mutation with nothing to
act on) rather than a silent success.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from kokoro_link.api.dependencies import get_container, require_admin
from kokoro_link.bootstrap.container import ServiceContainer
from kokoro_link.contracts.clock import ensure_utc
from kokoro_link.contracts.execution_mode import (
    MODE_DISTRIBUTED,
    MODE_EMBEDDED,
    MODE_PAUSED,
    VALID_MODES,
)
from kokoro_link.domain.entities.operator_profile import OperatorProfile

_LOGGER = logging.getLogger(__name__)

# Dedicated audit channel (§11) — greppable/routable independently.
_AUDIT_LOGGER = logging.getLogger("kokoro_link.audit.execution_mode")

router = APIRouter(
    tags=["execution-mode-admin"], dependencies=[Depends(require_admin)],
)


class ExecutionModeResponse(BaseModel):
    mode: str
    epoch: int


class FlipRequest(BaseModel):
    target_mode: str
    expected_epoch: int


class ObserveDrainRequest(BaseModel):
    paused_epoch: int


class DrainResponse(BaseModel):
    mode: str
    epoch: int
    confirmed: bool
    claimed: int | None = None


def _transition_service(container: ServiceContainer):
    return getattr(container, "execution_mode_transition", None)


def _raise_transition_error(result) -> None:
    status_code = (
        status.HTTP_409_CONFLICT
        if result.code in {"epoch_mismatch", "drain_not_confirmed", "already_paused", "paused_barrier_required"}
        else status.HTTP_503_SERVICE_UNAVAILABLE
    )
    raise HTTPException(
        status_code=status_code,
        detail={"code": result.code, "mode": result.mode, "epoch": result.epoch},
    )


@router.get(
    "/admin/background-execution-mode",
    response_model=ExecutionModeResponse,
)
async def get_execution_mode(
    container: ServiceContainer = Depends(get_container),
) -> ExecutionModeResponse:
    """Current mode + epoch. 503 when the ownership port is unwired in this
    process (registered-but-unwired parity, not a 404)."""
    port = getattr(container, "runtime_ownership", None)
    if port is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="runtime ownership port is not wired",
        )
    mode, epoch = await port.get()
    return ExecutionModeResponse(mode=mode, epoch=epoch)


@router.post(
    "/admin/background-execution-mode/flip",
    response_model=ExecutionModeResponse,
)
async def flip_execution_mode(
    body: FlipRequest,
    admin: OperatorProfile = Depends(require_admin),
    container: ServiceContainer = Depends(get_container),
) -> ExecutionModeResponse:
    """Enter paused or leave it only after the durable drain proof."""
    target_mode = body.target_mode.strip().lower()
    if target_mode not in VALID_MODES:
        _AUDIT_LOGGER.info(
            "execution mode flip admin=%s target=%s expected_epoch=%s result=invalid_mode",
            admin.id, body.target_mode, body.expected_epoch,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"invalid target mode; allowed: {sorted(VALID_MODES)}",
        )
    service = _transition_service(container)
    if service is None:
        _AUDIT_LOGGER.info(
            "execution mode flip admin=%s target=%s expected_epoch=%s result=unwired",
            admin.id, target_mode, body.expected_epoch,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="execution mode transition service is not wired",
        )
    if target_mode == MODE_PAUSED:
        result = await service.enter_paused(expected_epoch=body.expected_epoch)
    elif target_mode in (MODE_EMBEDDED, MODE_DISTRIBUTED):
        result = await service.leave_paused(
            target_mode, expected_epoch=body.expected_epoch,
        )
    else:
        _AUDIT_LOGGER.info(
            "execution mode flip admin=%s target=%s expected_epoch=%s result=direct_transition_forbidden",
            admin.id, target_mode, body.expected_epoch,
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "direct_transition_forbidden"},
        )
    _AUDIT_LOGGER.info(
        "execution mode flip admin=%s target=%s expected_epoch=%s result=%s mode=%s epoch=%s",
        admin.id, target_mode, body.expected_epoch, result.code if not result.ok else "flipped",
        result.mode, result.epoch,
    )
    if not result.ok:
        _raise_transition_error(result)
    return ExecutionModeResponse(mode=result.mode or target_mode, epoch=result.epoch or 0)


@router.post(
    "/admin/background-execution-mode/observe-drain",
    response_model=DrainResponse,
)
async def observe_drain(
    body: ObserveDrainRequest,
    admin: OperatorProfile = Depends(require_admin),
    container: ServiceContainer = Depends(get_container),
) -> DrainResponse:
    service = _transition_service(container)
    if service is None:
        _AUDIT_LOGGER.info(
            "execution mode drain observation admin=%s epoch=%s result=unwired",
            admin.id, body.paused_epoch,
        )
        raise HTTPException(status_code=503, detail="execution mode transition service is not wired")
    result = await service.observe_drain(paused_epoch=body.paused_epoch)
    _AUDIT_LOGGER.info(
        "execution mode drain observation admin=%s epoch=%s result=%s claimed=%s confirmed=%s",
        admin.id, body.paused_epoch, result.code, result.claimed, result.confirmed,
    )
    if not result.ok:
        _raise_transition_error(result)
    return DrainResponse(
        mode=result.mode or "paused", epoch=result.epoch or body.paused_epoch,
        claimed=result.claimed, confirmed=result.confirmed,
    )
