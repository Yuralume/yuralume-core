"""Cloud→Core internal channel for the LINE reactivation campaign (LR).

Sibling of :mod:`kokoro_link.api.routes.internal_cloud`: same versioned
service credential, same fail-closed posture, its own scope. The scope is
separate for the same reason ``card-freeze:write`` is — this surface
enumerates every dormant player's character across every tenant and (in
T2) fires messages at them, which is a different blast radius from a
tenant tier push, so a credential minted for those must not also fire
this one.

**部署前置**：``reactivation:write`` 必須加進兩側共用的 R1 credential
descriptor，Cloud 與 Core 同批更新——descriptor 少了它時，user service
的 proxy 呼叫一律 401。

Three endpoints: the candidate listing (T1), and the campaign start /
campaign report pair (T2). The start is ``202`` rather than ``201``
because the work it accepts is a background walk — the response says a
runner owns the selection, not that anything has been sent.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel

from kokoro_link.api.dependencies import get_container
from kokoro_link.api.routes.internal_cloud import (
    # Imported, not copied. ``external_chat.py`` duplicated this gate
    # because it needs a different error envelope; this router does not,
    # and a second copy of a credential check is a second place for
    # fail-closed behaviour to drift.
    _require_internal_cloud_credential,
)
from kokoro_link.application.services.line_reactivation import (
    LineReactivationCampaignService,
    LineReactivationCandidateService,
    LineReactivationEmptySelectionError,
    LineReactivationInvalidCampaignIdError,
    LineReactivationUnknownCharactersError,
)
from kokoro_link.bootstrap.container import ServiceContainer
from kokoro_link.contracts.line_reactivation import (
    LineReactivationCampaignConflictError,
)

router = APIRouter(
    prefix="/cloud/line-reactivation", tags=["internal-cloud"],
)

REACTIVATION_SCOPE = "reactivation:write"
"""Scope on the shared internal credential. Named ``:write`` even though
T1 only reads: the listing and the send are one operator capability, and
splitting them would mint a credential that can enumerate the roster
without being able to act on it — a distinction nothing downstream
makes."""

CAMPAIGN_CONFLICT_CODE = "campaign_conflict"
"""A ``campaign_id`` was re-used for a different selection (D5). Not a
retryable condition: the console must mint a new id, because the stored
ledger already describes another set of characters."""

EMPTY_SELECTION_CODE = "empty_selection"
"""``character_ids`` arrived empty. A client bug rather than an operator
choice — the console disables the button on an empty selection."""

INVALID_CAMPAIGN_ID_CODE = "invalid_campaign_id"
"""``campaign_id`` was blank or longer than the ledger column. Answered
as a 400 rather than allowed to reach the database, where it would arrive
as a driver error and be reported as a 500 — a malformed request must not
read as a broken server."""

INVALID_CHARACTERS_CODE = "invalid_characters"
"""The selection named characters with no row behind them — a candidate
list the operator loaded before someone deleted one of them, typically.
The body carries ``missing_character_ids`` so the console can drop those
rows and let the operator re-confirm, instead of showing a generic
failure for a selection that is mostly still valid. Retrying with a new
``campaign_id`` would not help, which is why this is not a 409."""

CLOUD_MODE_REQUIRED_CODE = "cloud_mode_required"
"""Returned by a self-host deployment. The campaign subsystem is wired
only in cloud mode: dormancy windows come from the control plane and the
send path is the Hosted Channel, neither of which exists self-host."""


async def require_internal_reactivation_credential(
    authorization: str | None = Header(default=None),
    service_token: str | None = Header(default=None, alias="X-Yuralume-Service-Token"),
    key_id: str | None = Header(default=None, alias="X-Yuralume-Service-Key-Id"),
    caller: str | None = Header(default=None, alias="X-Yuralume-Service-Caller"),
    audience: str | None = Header(default=None, alias="X-Yuralume-Service-Audience"),
    scope: str | None = Header(default=None, alias="X-Yuralume-Service-Scope"),
) -> None:
    await _require_internal_cloud_credential(
        required_scope=REACTIVATION_SCOPE,
        authorization=authorization,
        service_token=service_token,
        key_id=key_id,
        caller=caller,
        audience=audience,
        scope=scope,
    )


class CandidateResponse(BaseModel):
    character_id: str
    character_name: str
    user_id: str
    tier_key: str | None
    last_active_at: datetime
    dormancy_days: int
    dormant_for_days: int
    eligible: bool
    eligibility_reason: str | None


class CandidateListResponse(BaseModel):
    generated_at: datetime
    candidates: list[CandidateResponse]


class CampaignStartRequest(BaseModel):
    campaign_id: str
    character_ids: list[str]
    actor: str


class CampaignStartResponse(BaseModel):
    campaign_id: str
    status: str
    total: int
    resumed: bool


class CampaignItemResponse(BaseModel):
    character_id: str
    character_name: str
    outcome: str | None
    detail: str | None
    message_text: str | None
    """The verbatim body this character sent — the column the operator
    actually reads before releasing the rest of the selection.

    Contract: non-null **only** where ``outcome == "sent"``. Pending,
    skipped, blocked and errored rows carry ``null``, because a message
    that no player received cannot answer "does this land as a reunion?"
    """

    attempted_at: datetime | None


class CampaignReportResponse(BaseModel):
    campaign_id: str
    status: str
    actor: str
    created_at: datetime
    completed_at: datetime | None
    total: int
    done: int
    items: list[CampaignItemResponse]


def _cloud_mode_required() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={
            "code": CLOUD_MODE_REQUIRED_CODE,
            "message": (
                "line reactivation is a hosted-cloud capability and is "
                "not wired on this deployment"
            ),
        },
    )


def _require_service(
    container: ServiceContainer,
) -> LineReactivationCandidateService:
    service = container.line_reactivation_candidate_service
    if service is None:
        raise _cloud_mode_required()
    return service


def _require_campaign_service(
    container: ServiceContainer,
) -> LineReactivationCampaignService:
    service = container.line_reactivation_campaign_service
    if service is None:
        raise _cloud_mode_required()
    return service


@router.get(
    "/candidates",
    dependencies=[Depends(require_internal_reactivation_credential)],
    response_model=CandidateListResponse,
)
async def list_candidates(
    container: ServiceContainer = Depends(get_container),
) -> CandidateListResponse:
    """Dormant, LINE-reachable characters an operator may call back (D1).

    Includes characters the channel preflight says are *not* reachable,
    each with its reason — the console renders them greyed out rather
    than dropping them, so "why is this one missing?" is never a question
    the operator has to ask.
    """
    service = _require_service(container)
    listing = await service.list_candidates()
    return CandidateListResponse(
        generated_at=listing.generated_at,
        candidates=[
            CandidateResponse(
                character_id=candidate.character_id,
                character_name=candidate.character_name,
                user_id=candidate.user_id,
                tier_key=candidate.tier_key,
                last_active_at=candidate.last_active_at,
                dormancy_days=candidate.dormancy_days,
                dormant_for_days=candidate.dormant_for_days,
                eligible=candidate.eligible,
                eligibility_reason=candidate.eligibility_reason,
            )
            for candidate in listing.candidates
        ],
    )


@router.post(
    "/campaigns",
    dependencies=[Depends(require_internal_reactivation_credential)],
    response_model=CampaignStartResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_campaign(
    payload: CampaignStartRequest,
    container: ServiceContainer = Depends(get_container),
) -> CampaignStartResponse:
    """Accept a selection and hand it to a background serial runner (D5).

    Idempotent on ``campaign_id``: the same id with the same selection is
    a resume (``resumed: true``) and re-walks only the items that never
    got an outcome — which is also how a campaign survives a Core restart
    *and* how a POST that round-robins onto a second API replica stays
    safe, since each item is claimed before it is sent. The same id with
    a *different* selection is the one case that cannot be honoured, and
    answers 409 ``campaign_conflict``.

    The three 400s are all malformed-request shapes, each with its own
    code so the console can act on them: ``empty_selection``,
    ``invalid_campaign_id`` and ``invalid_characters`` (which carries
    ``missing_character_ids``).
    """
    service = _require_campaign_service(container)
    try:
        result = await service.start(
            campaign_id=payload.campaign_id,
            character_ids=payload.character_ids,
            actor=payload.actor,
        )
    except LineReactivationEmptySelectionError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": EMPTY_SELECTION_CODE, "message": str(exc)},
        ) from exc
    except LineReactivationInvalidCampaignIdError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": INVALID_CAMPAIGN_ID_CODE, "message": str(exc)},
        ) from exc
    except LineReactivationUnknownCharactersError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": INVALID_CHARACTERS_CODE,
                "message": str(exc),
                "missing_character_ids": list(exc.missing_character_ids),
            },
        ) from exc
    except LineReactivationCampaignConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": CAMPAIGN_CONFLICT_CODE, "message": str(exc)},
        ) from exc
    return CampaignStartResponse(
        campaign_id=result.campaign_id,
        status=result.status,
        total=result.total,
        resumed=result.resumed,
    )


@router.get(
    "/campaigns/{campaign_id}",
    dependencies=[Depends(require_internal_reactivation_credential)],
    response_model=CampaignReportResponse,
)
async def get_campaign(
    campaign_id: str,
    container: ServiceContainer = Depends(get_container),
) -> CampaignReportResponse:
    """The console's polling view: progress plus every per-character outcome.

    ``outcome is None`` means pending. Blocked and withheld rows are
    reported as they are rather than retried (D6) — a follow-up send is a
    new campaign, and the operator makes that call.

    Each ``sent`` row also carries ``message_text``: the full body that
    character sent, unclipped. That is what makes the intended workflow
    possible — fire a small batch, read what the characters actually
    said, then decide whether to release the rest.
    """
    service = _require_campaign_service(container)
    report = await service.report(campaign_id)
    if report is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "campaign_not_found",
                "message": f"no campaign {campaign_id!r}",
            },
        )
    return CampaignReportResponse(
        campaign_id=report.campaign_id,
        status=report.status,
        actor=report.actor,
        created_at=report.created_at,
        completed_at=report.completed_at,
        total=report.total,
        done=report.done,
        items=[
            CampaignItemResponse(
                character_id=item.character_id,
                character_name=item.character_name,
                outcome=item.outcome,
                detail=item.detail,
                message_text=item.message_text,
                attempted_at=item.attempted_at,
            )
            for item in report.items
        ],
    )
