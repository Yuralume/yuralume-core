"""Admin endpoints for the A/B experiment framework (HUMANIZATION_ROADMAP §4.6).

Owner decision (2026-05-21): the framework collects structured results
per bucket; winner detection is **manual** — operators trigger
``POST /admin/experiments/{id}/analyze`` to hand the per-bucket
snapshot to a high-tier LLM for a written report. **No auto traffic
switching, no auto winner declaration.**

Routes:

* ``POST /admin/experiments`` — create an experiment.
* ``GET /admin/experiments`` — list.
* ``GET /admin/experiments/{id}`` — fetch one.
* ``POST /admin/experiments/{id}/active`` — toggle active flag.
* ``POST /admin/experiments/{id}/assign`` — return / mint the variant
  for a (character, operator) pair.
* ``GET /admin/experiments/{id}/report`` — per-bucket assignment counts
  + optional subsystem-health slice payload.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from kokoro_link.api.dependencies import get_container, require_admin
from kokoro_link.bootstrap.container import ServiceContainer
from kokoro_link.domain.entities.operator_profile import DEFAULT_OPERATOR_ID


router = APIRouter(
    tags=["experiments"], dependencies=[Depends(require_admin)],
)


async def _resolve_operator_language(container: ServiceContainer) -> str:
    """Resolve the admin operator's content language for the analysis
    narrative. Falls back to the ship-first ``zh-TW`` when the profile
    service is unwired or the lookup fails."""
    default = "zh-TW"
    service = getattr(container, "operator_profile_service", None)
    if service is None:
        return default
    try:
        operator = await service.get_current()
    except Exception:  # pragma: no cover - defensive
        return default
    if operator is None:
        return default
    lang = (getattr(operator, "primary_language", "") or "").strip()
    return lang or default


class VariantPayload(BaseModel):
    id: str
    label: str = ""


class ExperimentResponse(BaseModel):
    id: str
    name: str
    description: str
    salt: str
    active: bool
    variants: list[VariantPayload]
    created_at: datetime


def _experiment_response(experiment) -> ExperimentResponse:  # noqa: ANN001
    return ExperimentResponse(
        id=experiment.id,
        name=experiment.name,
        description=experiment.description,
        salt=experiment.salt,
        active=experiment.active,
        variants=[
            VariantPayload(id=v.id, label=v.label) for v in experiment.variants
        ],
        created_at=experiment.created_at,
    )


class CreateExperimentRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=480)
    variant_ids: list[str] = Field(min_length=2, max_length=12)
    salt: str | None = None


@router.post("/admin/experiments", response_model=ExperimentResponse)
async def create_experiment(
    payload: CreateExperimentRequest,
    container: ServiceContainer = Depends(get_container),
) -> ExperimentResponse:
    svc = container.experiment_service
    if svc is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Experiment service is not wired",
        )
    experiment = await svc.create_experiment(
        name=payload.name,
        description=payload.description,
        variant_ids=payload.variant_ids,
        salt=payload.salt,
    )
    return _experiment_response(experiment)


@router.get("/admin/experiments", response_model=list[ExperimentResponse])
async def list_experiments(
    container: ServiceContainer = Depends(get_container),
) -> list[ExperimentResponse]:
    svc = container.experiment_service
    if svc is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Experiment service is not wired",
        )
    rows = await svc.list_experiments()
    return [_experiment_response(e) for e in rows]


@router.get("/admin/experiments/{experiment_id}", response_model=ExperimentResponse)
async def get_experiment(
    experiment_id: str,
    container: ServiceContainer = Depends(get_container),
) -> ExperimentResponse:
    svc = container.experiment_service
    if svc is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Experiment service is not wired",
        )
    experiment = await svc.get_experiment(experiment_id)
    if experiment is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No experiment with id={experiment_id!r}",
        )
    return _experiment_response(experiment)


class ToggleActiveRequest(BaseModel):
    active: bool


@router.post(
    "/admin/experiments/{experiment_id}/active",
    response_model=ExperimentResponse,
)
async def toggle_experiment_active(
    experiment_id: str,
    payload: ToggleActiveRequest,
    container: ServiceContainer = Depends(get_container),
) -> ExperimentResponse:
    svc = container.experiment_service
    if svc is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Experiment service is not wired",
        )
    ok = await svc.set_active(experiment_id, active=payload.active)
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No experiment with id={experiment_id!r}",
        )
    experiment = await svc.get_experiment(experiment_id)
    assert experiment is not None
    return _experiment_response(experiment)


class AssignRequest(BaseModel):
    character_id: str
    operator_id: str = DEFAULT_OPERATOR_ID


class AssignResponse(BaseModel):
    variant_id: str
    variant_label: str


@router.post(
    "/admin/experiments/{experiment_id}/assign",
    response_model=AssignResponse,
)
async def assign_variant(
    experiment_id: str,
    payload: AssignRequest,
    container: ServiceContainer = Depends(get_container),
) -> AssignResponse:
    svc = container.experiment_service
    if svc is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Experiment service is not wired",
        )
    variant = await svc.assign_variant(
        experiment_id=experiment_id,
        character_id=payload.character_id,
        operator_id=payload.operator_id,
    )
    if variant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"No active experiment with id={experiment_id!r} "
                "(experiment may be inactive or unknown)"
            ),
        )
    return AssignResponse(variant_id=variant.id, variant_label=variant.label)


class VariantBucketResponse(BaseModel):
    variant_id: str
    label: str
    assignment_count: int


class ExperimentReportResponse(BaseModel):
    experiment_id: str
    name: str
    description: str
    salt: str
    active: bool
    buckets: list[VariantBucketResponse]
    metadata: dict[str, Any] = Field(default_factory=dict)


@router.get(
    "/admin/experiments/{experiment_id}/report",
    response_model=ExperimentReportResponse,
)
async def experiment_report(
    experiment_id: str,
    container: ServiceContainer = Depends(get_container),
) -> ExperimentReportResponse:
    svc = container.experiment_service
    if svc is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Experiment service is not wired",
        )
    report = await svc.compile_report(experiment_id)
    if report is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No experiment with id={experiment_id!r}",
        )
    return ExperimentReportResponse(
        experiment_id=report.experiment_id,
        name=report.name,
        description=report.description,
        salt=report.salt,
        active=report.active,
        buckets=[
            VariantBucketResponse(
                variant_id=b.variant_id,
                label=b.label,
                assignment_count=b.assignment_count,
            )
            for b in report.buckets
        ],
        metadata=dict(report.metadata),
    )


class AnalyzeRequest(BaseModel):
    """Operator note to seed the manual LLM analysis prompt.

    Owner decision (2026-05-21): analyze step is **manual** — this
    endpoint just persists the operator's intent and returns a stub
    response telling them where to find the structured payload. Wiring
    to a high-tier LLM model is intentionally a follow-up step so the
    owner can choose model + prompt at trigger time.
    """

    note: str = Field(default="", max_length=480)


class AnalyzeResponse(BaseModel):
    experiment_id: str
    queued: bool
    invoked_model: str
    narrative: str
    structured_payload: dict[str, Any]
    error: str | None = None
    message: str = ""


@router.post(
    "/admin/experiments/{experiment_id}/analyze",
    response_model=AnalyzeResponse,
)
async def run_analysis(
    experiment_id: str,
    payload: AnalyzeRequest,
    container: ServiceContainer = Depends(get_container),
) -> AnalyzeResponse:
    """HUMANIZATION_ROADMAP §4.6 — manual high-tier LLM comparison.

    Owner guard (2026-05-21): the endpoint **is** manually triggered
    (operator hits this URL via UI / CLI / curl); but once invoked it
    actually calls the high-tier model rather than returning a stub
    message. The prompt fed to the model explicitly forbids declaring
    a winner — the narrative is supportive material for human eyes."""
    svc = container.experiment_service
    if svc is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Experiment service is not wired",
        )
    experiment = await svc.get_experiment(experiment_id)
    if experiment is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No experiment with id={experiment_id!r}",
        )
    analysis_service = container.experiment_analysis_service
    if analysis_service is None:
        return AnalyzeResponse(
            experiment_id=experiment_id,
            queued=False,
            invoked_model="",
            narrative="",
            structured_payload={},
            message=(
                "ExperimentAnalysisService not wired; cannot dispatch LLM. "
                "Fetch the structured payload from GET "
                f"/admin/experiments/{experiment_id}/report manually."
            ),
        )
    # The narrative renders in the admin UI, so pin its output language to
    # the operator's primary language instead of a hardcoded 繁體中文 mandate.
    operator_language = await _resolve_operator_language(container)
    result = await analysis_service.analyze(
        experiment_id=experiment_id,
        operator_note=payload.note,
        operator_primary_language=operator_language,
    )
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No experiment with id={experiment_id!r}",
        )
    return AnalyzeResponse(
        experiment_id=result.experiment_id,
        queued=True,
        invoked_model=result.invoked_model,
        narrative=result.narrative,
        structured_payload=result.structured_payload,
        error=result.error,
        message=(
            "manual analysis dispatched; narrative is supportive material — "
            "the server does not declare a winner (owner 2026-05-21)."
        ),
    )
