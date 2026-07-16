from __future__ import annotations

from pathlib import Path

import pytest

from kokoro_link.application.dto.character import CreateCharacterRequest
from kokoro_link.application.services.character_card_export_service import (
    CharacterCardExportService,
)
from kokoro_link.application.services.character_card_import_service import (
    CharacterCardImportService,
)
from kokoro_link.application.services.character_image_service import (
    CharacterImageService,
)
from kokoro_link.application.services.character_service import CharacterService
from kokoro_link.domain.entities.arc_series import ArcSeries
from kokoro_link.domain.entities.arc_template import ArcTemplate, ArcTemplateBeat
from kokoro_link.domain.entities.operator_profile import DEFAULT_OPERATOR_ID
from kokoro_link.infrastructure.repositories.in_memory_arc_series import (
    InMemoryArcSeriesRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_arc_templates import (
    InMemoryArcTemplateRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)
from kokoro_link.infrastructure.storage.in_memory import InMemoryObjectStorage


_IMPORTER = "importer-user"


def _template(template_id: str, title: str) -> ArcTemplate:
    return ArcTemplate.create(
        id=template_id,
        title=title,
        premise=f"{title} premise",
        theme="coming-of-age",
        tone="hopeful",
        beats=[
            ArcTemplateBeat.create(
                sequence=0,
                day_offset=0,
                title=f"{title} opening",
                summary="A playable setup beat.",
                tension="setup",
            ),
        ],
    )


async def _export_series_card() -> bytes:
    char_repo = InMemoryCharacterRepository()
    character_service = CharacterService(char_repo)
    template_repo = InMemoryArcTemplateRepository()
    series_repo = InMemoryArcSeriesRepository()
    export_service = CharacterCardExportService(
        character_service=character_service,
        object_storage=InMemoryObjectStorage(public_base_url="/source"),
        arc_template_repository=template_repo,
        arc_series_repository=series_repo,
    )
    for template in (
        _template("pilot_arc", "Pilot Arc"),
        _template("finale_arc", "Finale Arc"),
    ):
        await template_repo.save_for_user(template, user_id=DEFAULT_OPERATOR_ID)
    await series_repo.save_for_user(
        ArcSeries.create(
            id="starlight_series",
            title="Starlight Series",
            premise="A fixed two-season journey.",
            template_ids=["pilot_arc", "finale_arc"],
            user_id=DEFAULT_OPERATOR_ID,
        ),
        user_id=DEFAULT_OPERATOR_ID,
    )
    created = await character_service.create_character(
        CreateCharacterRequest(
            name="Mio",
            summary="A singer with a planned two-season arc.",
            arc_template_id="pilot_arc",
            arc_series_id="starlight_series",
        ),
        user_id=DEFAULT_OPERATOR_ID,
    )
    return (await export_service.export(created.id, user_id=DEFAULT_OPERATOR_ID)).blob


def _build_import_service() -> tuple[
    CharacterCardImportService,
    InMemoryArcTemplateRepository,
    InMemoryArcSeriesRepository,
]:
    char_repo = InMemoryCharacterRepository()
    character_service = CharacterService(char_repo)
    storage = InMemoryObjectStorage(public_base_url="/target")
    image_service = CharacterImageService(
        character_repository=char_repo,
        uploads_dir=Path("."),
        object_storage=storage,
    )
    template_repo = InMemoryArcTemplateRepository()
    series_repo = InMemoryArcSeriesRepository()
    service = CharacterCardImportService(
        character_service=character_service,
        character_image_service=image_service,
        arc_template_repository=template_repo,
        arc_series_repository=series_repo,
    )
    return service, template_repo, series_repo


@pytest.mark.asyncio
async def test_import_lands_bundled_series_and_binds_character_without_progress() -> None:
    blob = await _export_series_card()
    service, _template_repo, series_repo = _build_import_service()

    result = await service.import_card(blob, user_id=_IMPORTER)

    assert result.landed_arc_series_ids == ["starlight_series"]
    assert result.character.arc_series_id == "starlight_series"
    landed = await series_repo.get_for_user("starlight_series", user_id=_IMPORTER)
    assert landed is not None
    assert landed.user_id == _IMPORTER
    assert landed.member_template_ids == ("pilot_arc", "finale_arc")
    assert await series_repo.get_progress(result.character.id, landed.id) is None


@pytest.mark.asyncio
async def test_import_remaps_series_and_member_templates_on_collisions() -> None:
    blob = await _export_series_card()
    service, template_repo, series_repo = _build_import_service()
    for template in (
        _template("pilot_arc", "Existing Pilot"),
        _template("finale_arc", "Existing Finale"),
    ):
        await template_repo.save_for_user(template, user_id=_IMPORTER)
    await series_repo.save_for_user(
        ArcSeries.create(
            id="starlight_series",
            title="Existing Series",
            premise="Importer-owned series.",
            template_ids=["pilot_arc", "finale_arc"],
            user_id=_IMPORTER,
        ),
        user_id=_IMPORTER,
    )

    result = await service.import_card(blob, user_id=_IMPORTER)

    assert any(
        template_id.startswith("pilot_arc-")
        for template_id in result.landed_arc_template_ids
    )
    assert any(
        template_id.startswith("finale_arc-")
        for template_id in result.landed_arc_template_ids
    )
    landed_series_id = result.landed_arc_series_ids[0]
    assert landed_series_id.startswith("starlight_series-")
    assert result.character.arc_series_id == landed_series_id
    landed = await series_repo.get_for_user(landed_series_id, user_id=_IMPORTER)
    assert landed is not None
    assert landed.member_template_ids[0].startswith("pilot_arc-")
    assert landed.member_template_ids[1].startswith("finale_arc-")
    assert set(landed.member_template_ids) == set(result.landed_arc_template_ids)
