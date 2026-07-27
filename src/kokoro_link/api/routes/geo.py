"""City-name search proxy for the hosted "where I live" picker (plan G2).

Hosted players must not be asked to type latitude / longitude — that is a
technical artefact, not a place. The SPA sends a city name here and gets
back ready-to-store facts.

Proxied rather than called from the browser on purpose: it keeps the third
party off the SPA's connect-src, keeps the endpoint swappable without a
front-end release, and lets the search language follow the operator's own
content language instead of the browser's.

Cloud mode only (404 otherwise), matching ``GET /cloud/credits``: the
self-host location form keeps its existing manual fields untouched.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status

from kokoro_link.api.contracts.player_locale import (
    GeoSearchResponse,
    GeoSearchResultResponse,
)
from kokoro_link.api.dependencies import (
    get_container,
    get_current_user,
    is_cloud_mode,
)
from kokoro_link.bootstrap.container import ServiceContainer
from kokoro_link.domain.entities.operator_profile import OperatorProfile

router = APIRouter(prefix="/geo", tags=["geo"])

MAX_RESULTS = 5


@router.get("/search", response_model=GeoSearchResponse)
async def search_places(
    q: str = Query(default="", max_length=128, description="City / place name."),
    user: OperatorProfile = Depends(get_current_user),
    container: ServiceContainer = Depends(get_container),
) -> GeoSearchResponse:
    """Suggest places for ``q`` in the caller's content language.

    Fail-soft by construction: a blank / too-short query, an unwired
    client, or an upstream outage all answer ``200`` with an empty list, so
    a search box never turns into an error banner mid-typing."""
    if not is_cloud_mode(container):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="place search is only available in cloud mode",
        )
    client = getattr(container, "geocoding_client", None)
    if client is None:
        return GeoSearchResponse(results=[])
    places = await client.search(
        q,
        language=getattr(user, "primary_language", None),
        limit=MAX_RESULTS,
    )
    return GeoSearchResponse(
        results=[
            GeoSearchResultResponse(
                label=place.label,
                latitude=place.latitude,
                longitude=place.longitude,
                country_code=place.country_code,
                timezone_id=place.timezone_id,
            )
            for place in places
        ],
    )
