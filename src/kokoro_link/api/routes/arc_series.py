"""ArcSeries REST routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from kokoro_link.api.dependencies import get_container, get_current_user_id
from kokoro_link.api.operator_language import (
    resolve_stored_operator_primary_language,
)
from kokoro_link.api.routes.arc_template_intake import TemplateDraftPayload
from kokoro_link.application.services.arc_series_service import (
    ArcSeriesNotFoundError,
    ArcSeriesService,
    ArcSeriesValidationError,
)
from kokoro_link.bootstrap.container import ServiceContainer
from kokoro_link.domain.entities.arc_series import (
    ArcSeries,
    CharacterSeriesProgress,
)

router = APIRouter(tags=["arc-series"])


class ArcSeriesBindingSummary(BaseModel):
    world_frames: list[str]
    required_traits: list[str]


class ArcSeriesMemberSummary(BaseModel):
    template_id: str
    position: int


class ArcSeriesResponse(BaseModel):
    id: str
    title: str
    premise: str
    theme: str
    tone: str
    binding: ArcSeriesBindingSummary
    members: list[ArcSeriesMemberSummary]
    member_count: int
    is_pack: bool

    @classmethod
    def from_domain(cls, series: ArcSeries) -> "ArcSeriesResponse":
        return cls(
            id=series.id,
            title=series.title,
            premise=series.premise,
            theme=series.theme,
            tone=series.tone,
            binding=ArcSeriesBindingSummary(
                world_frames=list(series.binding.world_frames),
                required_traits=list(series.binding.required_traits),
            ),
            members=[
                ArcSeriesMemberSummary(
                    template_id=member.template_id,
                    position=member.position,
                )
                for member in series.members
            ],
            member_count=len(series.members),
            is_pack=series.is_pack,
        )


class ArcSeriesProgressResponse(BaseModel):
    character_id: str
    series_id: str
    current_index: int
    status: str
    last_arc_id: str | None

    @classmethod
    def from_domain(
        cls, progress: CharacterSeriesProgress,
    ) -> "ArcSeriesProgressResponse":
        return cls(
            character_id=progress.character_id,
            series_id=progress.series_id,
            current_index=progress.current_index,
            status=progress.status,
            last_arc_id=progress.last_arc_id,
        )


class ArcSeriesRequest(BaseModel):
    id: str | None = Field(default=None, min_length=1, max_length=64)
    title: str = Field(min_length=1)
    premise: str = Field(min_length=1)
    theme: str = "custom"
    tone: str = "dramatic"
    world_frames: list[str] = Field(default_factory=list)
    required_traits: list[str] = Field(default_factory=list)
    template_ids: list[str] = Field(min_length=2)


class ReorderArcSeriesRequest(BaseModel):
    template_ids: list[str] = Field(min_length=2)


class BindArcSeriesRequest(BaseModel):
    character_id: str = Field(min_length=1)


class DraftNextSeasonRequest(BaseModel):
    character_id: str = Field(min_length=1)
    instruction: str = ""
    selected_memory_ids: list[str] = Field(default_factory=list)


def _require_service(container: ServiceContainer) -> ArcSeriesService:
    if container.arc_series_service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Arc series service not configured",
        )
    return container.arc_series_service


def _require_continuation_service(container: ServiceContainer):
    service = container.arc_series_continuation_draft_service
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Arc series continuation service not configured",
        )
    return service


def _map_service_error(exc: Exception) -> HTTPException:
    if isinstance(exc, ArcSeriesNotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    if isinstance(exc, ArcSeriesValidationError):
        return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail=str(exc),
    )


@router.get("/arc-series", response_model=list[ArcSeriesResponse])
async def list_arc_series(
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> list[ArcSeriesResponse]:
    service = _require_service(container)
    series = await service.list_for_user(current_user_id)
    return [ArcSeriesResponse.from_domain(item) for item in series]


@router.post(
    "/arc-series",
    response_model=ArcSeriesResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_arc_series(
    payload: ArcSeriesRequest,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> ArcSeriesResponse:
    service = _require_service(container)
    try:
        series = await service.create_for_user(
            user_id=current_user_id,
            id=payload.id,
            title=payload.title,
            premise=payload.premise,
            theme=payload.theme,
            tone=payload.tone,
            world_frames=payload.world_frames,
            required_traits=payload.required_traits,
            template_ids=payload.template_ids,
        )
    except (ArcSeriesNotFoundError, ArcSeriesValidationError) as exc:
        raise _map_service_error(exc) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    return ArcSeriesResponse.from_domain(series)


@router.get("/arc-series/{series_id}", response_model=ArcSeriesResponse)
async def get_arc_series(
    series_id: str,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> ArcSeriesResponse:
    service = _require_service(container)
    try:
        series = await service.get_for_user(series_id, user_id=current_user_id)
    except ArcSeriesNotFoundError as exc:
        raise _map_service_error(exc) from exc
    return ArcSeriesResponse.from_domain(series)


@router.post(
    "/arc-series/{series_id}/draft-next-season",
    response_model=TemplateDraftPayload,
)
async def draft_next_season(
    series_id: str,
    payload: DraftNextSeasonRequest,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> TemplateDraftPayload:
    service = _require_continuation_service(container)
    try:
        draft = await service.draft_next_season(
            series_id=series_id,
            character_id=payload.character_id,
            user_id=current_user_id,
            instruction=payload.instruction,
            selected_memory_ids=payload.selected_memory_ids,
            operator_primary_language=(
                await resolve_stored_operator_primary_language(
                    container,
                    current_user_id,
                )
            ),
        )
    except ArcSeriesNotFoundError as exc:
        raise _map_service_error(exc) from exc
    except ArcSeriesValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    if draft is None:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="The model did not return a continuation draft",
        )
    return TemplateDraftPayload.from_domain(draft)


@router.patch("/arc-series/{series_id}", response_model=ArcSeriesResponse)
async def update_arc_series(
    series_id: str,
    payload: ArcSeriesRequest,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> ArcSeriesResponse:
    service = _require_service(container)
    try:
        series = await service.update_for_user(
            series_id,
            user_id=current_user_id,
            title=payload.title,
            premise=payload.premise,
            theme=payload.theme,
            tone=payload.tone,
            world_frames=payload.world_frames,
            required_traits=payload.required_traits,
            template_ids=payload.template_ids,
        )
    except (ArcSeriesNotFoundError, ArcSeriesValidationError) as exc:
        raise _map_service_error(exc) from exc
    return ArcSeriesResponse.from_domain(series)


@router.post("/arc-series/{series_id}/reorder", response_model=ArcSeriesResponse)
async def reorder_arc_series(
    series_id: str,
    payload: ReorderArcSeriesRequest,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> ArcSeriesResponse:
    service = _require_service(container)
    try:
        series = await service.reorder_for_user(
            series_id,
            user_id=current_user_id,
            template_ids=payload.template_ids,
        )
    except (ArcSeriesNotFoundError, ArcSeriesValidationError) as exc:
        raise _map_service_error(exc) from exc
    return ArcSeriesResponse.from_domain(series)


@router.delete(
    "/arc-series/{series_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_arc_series(
    series_id: str,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> None:
    service = _require_service(container)
    try:
        await service.delete_for_user(series_id, user_id=current_user_id)
    except ArcSeriesNotFoundError as exc:
        raise _map_service_error(exc) from exc


@router.post(
    "/arc-series/{series_id}/bind-to-character",
    response_model=ArcSeriesResponse,
)
async def bind_arc_series_to_character(
    series_id: str,
    payload: BindArcSeriesRequest,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> ArcSeriesResponse:
    service = _require_service(container)
    try:
        series = await service.bind_to_character(
            character_id=payload.character_id,
            series_id=series_id,
            user_id=current_user_id,
        )
    except (ArcSeriesNotFoundError, ArcSeriesValidationError) as exc:
        raise _map_service_error(exc) from exc
    if series is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Series binding unexpectedly returned no series",
        )
    return ArcSeriesResponse.from_domain(series)


@router.delete(
    "/characters/{character_id}/arc-series-binding",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def clear_arc_series_binding(
    character_id: str,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> None:
    service = _require_service(container)
    try:
        await service.bind_to_character(
            character_id=character_id,
            series_id=None,
            user_id=current_user_id,
        )
    except (ArcSeriesNotFoundError, ArcSeriesValidationError) as exc:
        raise _map_service_error(exc) from exc


@router.get(
    "/characters/{character_id}/arc-series-progress/{series_id}",
    response_model=ArcSeriesProgressResponse | None,
)
async def get_arc_series_progress(
    character_id: str,
    series_id: str,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> ArcSeriesProgressResponse | None:
    service = _require_service(container)
    try:
        progress = await service.progress_for_character(
            character_id=character_id,
            series_id=series_id,
            user_id=current_user_id,
        )
    except ArcSeriesNotFoundError as exc:
        raise _map_service_error(exc) from exc
    return (
        ArcSeriesProgressResponse.from_domain(progress)
        if progress is not None
        else None
    )
