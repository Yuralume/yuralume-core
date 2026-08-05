"""Character-card marketplace routes (M4).

Lists the character cards this deployment can offer — the official ones
from the Cloud catalog plus any local ``.lumecard`` packs — and installs a
chosen one as a brand-new character owned by the caller. Install reuses the
import path, so a marketplace install and a manual ``.lumecard`` upload
behave identically (A-layer only; runtime never travels with the card; the
local install request may attach an importer-confirmed starting
relationship). See ``docs/CHARACTER_CARD_PLAN.md`` §7–§8.

The two sources answer through one shape. A local pack is read off disk; an
official card is fetched from the public Yuralume catalog
(``KOKORO_OFFICIAL_CARD_CATALOG_URL`` — an anonymous ``GET``, on by default,
turned off completely by setting it empty) and arrives with its prose
already translated and approved, so installing one never calls a model. An
unreachable or disabled catalog is an empty official shelf plus a flag,
never an error — local packs and installed characters are unaffected.

List, preview and install resolve the caller's stored ``primary_language`` —
the content language, not the UI locale — because that is what decides which
approved translation the official catalog answers with. It is read even when
nothing is being translated, so one code path serves both sources; the
alternative was branching on the id prefix in three routes.
"""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Body, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field

from kokoro_link.api.character_runtime import (
    ensure_character_primary_image,
    enqueue_character_runtime_initialization,
)
from kokoro_link.api.dependencies import get_container, get_current_user_id
from kokoro_link.api.operator_language import (
    resolve_stored_operator_primary_language,
)
from kokoro_link.application.dto.character import (
    CharacterResponse,
    InitialRelationshipPayload,
)
from kokoro_link.application.dto.character_card import CharacterCardPreview
from kokoro_link.application.services.character_card_import_service import (
    CharacterCardImportError,
    UnsupportedCardSchemaError,
)
from kokoro_link.application.services.character_card_pack_service import (
    CharacterCardPackNotFoundError,
    CharacterCardPackSummary,
)
from kokoro_link.application.services.character_service import (
    CharacterValidationError,
)
from kokoro_link.application.services.official_card_pack_source import (
    OfficialCardUnavailableError,
)
from kokoro_link.bootstrap.container import ServiceContainer
from kokoro_link.infrastructure.character_card.packager import (
    InvalidCharacterCardError,
)

router = APIRouter(tags=["character-cards"])


class CharacterCardCatalogResponse(BaseModel):
    """The card shelf, plus whether part of it is missing right now.

    ``official_cards_unavailable`` is how a Cloud outage reaches the player:
    the official rows are absent and the UI can say so, instead of a failed
    request that would also hide the local packs and look like the feature
    broke.
    """

    cards: list[CharacterCardPackSummary] = Field(default_factory=list)
    official_cards_unavailable: bool = False


class InstallCharacterCardResponse(BaseModel):
    """Result of installing a marketplace pack: the new character plus
    any arc templates landed alongside it (post collision remap)."""

    character: CharacterResponse
    landed_arc_template_ids: list[str] = Field(default_factory=list)
    landed_arc_series_ids: list[str] = Field(default_factory=list)


class InstallCharacterCardRequest(BaseModel):
    translate: bool | None = None
    initial_relationship: InitialRelationshipPayload | None = None


def _unreadable_card(exc: Exception) -> HTTPException:
    """Map an importer refusal onto the status a manual upload already gets.

    Both doors into the import pipeline — ``POST /characters/import`` with a
    file, and a marketplace install — run the same unpack + schema
    validation, so they must fail the same way. A card that this build
    cannot read is 422 (too new to understand) or 400 (not a readable card
    at all); it is never a 500, which would tell the player Yuralume broke
    and tell the owner nothing about which card is the problem.

    This is reachable from Cloud, not only from an upload: the catalog
    publishes the bytes and *this* build decides whether it can read them, so
    an artifact from a newer exporter arrives here through a path where
    nobody uploaded anything.
    """
    status_code = (
        status.HTTP_422_UNPROCESSABLE_ENTITY
        if isinstance(exc, UnsupportedCardSchemaError)
        else status.HTTP_400_BAD_REQUEST
    )
    return HTTPException(status_code=status_code, detail=str(exc))


def _require_pack_service(container: ServiceContainer):
    service = container.character_card_pack_service
    if service is None:  # pragma: no cover — wired in build_container
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Character card marketplace is not available",
        )
    return service


@router.get("/character-cards", response_model=CharacterCardCatalogResponse)
async def list_character_cards(
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> CharacterCardCatalogResponse:
    """List the character cards available to install, official ones first."""
    catalogue = await _require_pack_service(container).list_available(
        primary_language=await resolve_stored_operator_primary_language(
            container, current_user_id,
        ),
    )
    return CharacterCardCatalogResponse(
        cards=catalogue.cards,
        official_cards_unavailable=catalogue.official_cards_unavailable,
    )


@router.get("/character-cards/{pack_id}/images/{index}")
async def get_character_card_image(
    pack_id: str,
    index: int,
    container: ServiceContainer = Depends(get_container),
    _user_id: str = Depends(get_current_user_id),
) -> Response:
    """Stream one stage image from a bundled character-card pack."""
    service = _require_pack_service(container)
    try:
        image = service.get_image(pack_id, index)
    except CharacterCardPackNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Character card image not found",
        ) from exc
    return Response(
        content=image.data,
        media_type=image.content_type,
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.post(
    "/character-cards/{pack_id}/preview",
    response_model=CharacterCardPreview,
)
async def preview_character_card_pack(
    pack_id: str,
    translate: bool = Query(default=False),
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> CharacterCardPreview:
    """Preview one card, optionally translated for this operator."""
    service = _require_pack_service(container)
    language = await resolve_stored_operator_primary_language(
        container, current_user_id,
    )
    try:
        return await service.preview(
            pack_id,
            translate=translate,
            target_language=language if translate else "",
            primary_language=language,
        )
    except CharacterCardPackNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Character card pack not found",
        ) from exc
    except OfficialCardUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Official card catalog is temporarily unavailable",
        ) from exc
    except (InvalidCharacterCardError, CharacterCardImportError) as exc:
        raise _unreadable_card(exc) from exc


@router.post(
    "/character-cards/{pack_id}/install",
    response_model=InstallCharacterCardResponse,
)
async def install_character_card(
    pack_id: str,
    background_tasks: BackgroundTasks,
    translate: bool = Query(default=False),
    install_request: InstallCharacterCardRequest | None = Body(default=None),
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> InstallCharacterCardResponse:
    """Install a catalogue card as a new character owned by the caller."""
    service = _require_pack_service(container)
    effective_translate = (
        install_request.translate
        if install_request and install_request.translate is not None
        else translate
    )
    language = await resolve_stored_operator_primary_language(
        container, current_user_id,
    )
    try:
        result = await service.install(
            pack_id,
            user_id=current_user_id,
            translate=effective_translate,
            target_language=language if effective_translate else "",
            primary_language=language,
            initial_relationship=(
                install_request.initial_relationship if install_request else None
            ),
        )
    except CharacterCardPackNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Character card pack not found",
        ) from exc
    except OfficialCardUnavailableError as exc:
        # Never a 500: the catalog is a mirror this deployment reads, and a
        # bad minute upstream is a "try again", not a failure of Yuralume.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Official card catalog is temporarily unavailable",
        ) from exc
    except (InvalidCharacterCardError, CharacterCardImportError) as exc:
        # The download worked and the card is what failed — a different
        # thing to say than 503, and still not a 500.
        raise _unreadable_card(exc) from exc
    except CharacterValidationError as exc:
        # An install *creates a character*, so it walks into the same wall
        # ``POST /characters`` does — an account's character-slot cap, or its
        # daily create limit. A player at their limit clicking an official
        # card is an ordinary, expected refusal; it reads as 400 here for the
        # same reason it does there, rather than as a crash.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc),
        ) from exc
    enqueue_character_runtime_initialization(
        background_tasks,
        container=container,
        character=result.character,
        user_id=current_user_id,
    )
    character = await ensure_character_primary_image(
        container=container,
        character=result.character,
        user_id=current_user_id,
    )
    return InstallCharacterCardResponse(
        character=character,
        landed_arc_template_ids=result.landed_arc_template_ids,
        landed_arc_series_ids=result.landed_arc_series_ids,
    )
