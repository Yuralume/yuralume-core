"""Admin endpoints for the RSS / world-event pipeline.

Lets an operator trigger the ingest or curate pass on demand instead
of waiting for the WorldEventScheduler tick (default 30 / 60 min).
"""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from kokoro_link.api.dependencies import get_container, require_admin
from kokoro_link.bootstrap.container import ServiceContainer
from kokoro_link.domain.entities.rss_source import RssSource

router = APIRouter(
    prefix="/admin/world-events",
    tags=["world-events"],
    dependencies=[Depends(require_admin)],
)


class FeedCreateRequest(BaseModel):
    """Create a world-event RSS feed (CORE_ENV_TO_ADMIN_CONFIG track 3).

    Replaces the deprecated ``KOKORO_WORLD_EVENT_FEED_*`` env family — the
    operator adds feeds from the Admin 站點設定 → World events panel."""

    id: str = Field(min_length=1, max_length=64)
    name: str = ""
    feed_url: str = Field(min_length=1)
    category: str = "news"
    locale: str = "zh-TW"
    enabled: bool = True


class FeedUpdateRequest(BaseModel):
    """Partial update: any omitted field keeps its current value."""

    name: str | None = None
    feed_url: str | None = None
    category: str | None = None
    locale: str | None = None
    enabled: bool | None = None


@router.get("/sources")
async def list_sources(
    container: ServiceContainer = Depends(get_container),
) -> dict:
    repository = container.rss_source_repository
    if repository is None:
        raise HTTPException(503, "rss_source_repository unavailable")
    sources = await repository.list_all()
    return {
        "sources": [_source_status_payload(source) for source in sources],
        "total": len(sources),
        "enabled": sum(1 for source in sources if source.enabled),
        "failing": sum(1 for source in sources if _health_status(source) == "failing"),
    }


@router.post("/sources", status_code=201)
async def create_source(
    payload: FeedCreateRequest,
    container: ServiceContainer = Depends(get_container),
) -> dict:
    repository = container.rss_source_repository
    if repository is None:
        raise HTTPException(503, "rss_source_repository unavailable")
    if await repository.get(payload.id) is not None:
        raise HTTPException(409, f"feed id already exists: {payload.id}")
    try:
        source = RssSource(
            id=payload.id,
            name=payload.name or payload.id,
            feed_url=payload.feed_url,
            category=payload.category,
            locale=payload.locale,
            enabled=payload.enabled,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    await repository.upsert(source)
    return _source_status_payload(source)


@router.patch("/sources/{source_id}")
async def update_source(
    source_id: str,
    payload: FeedUpdateRequest,
    container: ServiceContainer = Depends(get_container),
) -> dict:
    repository = container.rss_source_repository
    if repository is None:
        raise HTTPException(503, "rss_source_repository unavailable")
    current = await repository.get(source_id)
    if current is None:
        raise HTTPException(404, f"unknown feed: {source_id}")
    from dataclasses import replace

    try:
        updated = replace(
            current,
            name=payload.name if payload.name is not None else current.name,
            feed_url=payload.feed_url
            if payload.feed_url is not None
            else current.feed_url,
            category=payload.category
            if payload.category is not None
            else current.category,
            locale=payload.locale if payload.locale is not None else current.locale,
            enabled=payload.enabled
            if payload.enabled is not None
            else current.enabled,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    await repository.upsert(updated)
    return _source_status_payload(updated)


@router.delete("/sources/{source_id}", status_code=204)
async def delete_source(
    source_id: str,
    container: ServiceContainer = Depends(get_container),
) -> None:
    repository = container.rss_source_repository
    if repository is None:
        raise HTTPException(503, "rss_source_repository unavailable")
    if await repository.get(source_id) is None:
        raise HTTPException(404, f"unknown feed: {source_id}")
    await repository.delete(source_id)


@router.post("/ingest")
async def trigger_ingest(
    container: ServiceContainer = Depends(get_container),
) -> dict:
    scheduler = container.world_event_scheduler
    if scheduler is None:
        raise HTTPException(503, "world_event_scheduler unavailable")
    report = await scheduler.trigger_ingest_now()
    return {
        "sources_attempted": report.sources_attempted,
        "sources_succeeded": report.sources_succeeded,
        "events_persisted": report.events_persisted,
        "events_skipped_dedup": report.events_skipped_dedup,
        "events_skipped_embed": report.events_skipped_embed,
        "errors": list(report.errors),
    }


@router.post("/curate")
async def trigger_curate(
    container: ServiceContainer = Depends(get_container),
) -> dict:
    scheduler = container.world_event_scheduler
    if scheduler is None:
        raise HTTPException(503, "world_event_scheduler unavailable")
    results = await scheduler.trigger_curate_now()
    return {
        "characters": len(results),
        "total_added": sum(r["added"] for r in results if r["added"] >= 0),
        "per_character": results,
    }


def _source_status_payload(source: RssSource) -> dict:
    return {
        "id": source.id,
        "name": source.name,
        "feed_url": source.feed_url,
        "category": source.category,
        "locale": source.locale,
        "enabled": source.enabled,
        "health_status": _health_status(source),
        "last_success_at": _isoformat(source.last_success_at),
        "last_attempt_at": _isoformat(source.last_attempt_at),
        "last_error": source.last_error,
        "fetched_count_total": source.fetched_count_total,
    }


def _health_status(source: RssSource) -> str:
    if not source.enabled:
        return "disabled"
    if source.last_error:
        return "failing"
    if source.last_success_at:
        return "healthy"
    return "unknown"


def _isoformat(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()
