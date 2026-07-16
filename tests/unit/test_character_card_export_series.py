from __future__ import annotations

import pytest

from kokoro_link.application.dto.character import CreateCharacterRequest
from kokoro_link.application.services.character_card_export_service import (
    CharacterCardExportService,
)
from kokoro_link.application.services.character_service import CharacterService
from kokoro_link.domain.entities.arc_series import ArcSeries
from kokoro_link.domain.entities.arc_template import ArcTemplate, ArcTemplateBeat
from kokoro_link.domain.entities.operator_profile import DEFAULT_OPERATOR_ID
from kokoro_link.infrastructure.character_card.packager import unpack_character_card
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


@pytest.mark.asyncio
async def test_export_bundles_bound_arc_series_and_member_templates() -> None:
    char_repo = InMemoryCharacterRepository()
    character_service = CharacterService(char_repo)
    template_repo = InMemoryArcTemplateRepository()
    series_repo = InMemoryArcSeriesRepository()
    service = CharacterCardExportService(
        character_service=character_service,
        object_storage=InMemoryObjectStorage(public_base_url="/uploads"),
        arc_template_repository=template_repo,
        arc_series_repository=series_repo,
    )
    for template in (
        _template("pilot_arc", "Pilot Arc"),
        _template("finale_arc", "Finale Arc"),
    ):
        await template_repo.save_for_user(template, user_id=DEFAULT_OPERATOR_ID)
    series = ArcSeries.create(
        id="starlight_series",
        title="Starlight Series",
        premise="A fixed two-season journey.",
        theme="ambition",
        tone="warm",
        template_ids=["pilot_arc", "finale_arc"],
        user_id=DEFAULT_OPERATOR_ID,
    )
    await series_repo.save_for_user(series, user_id=DEFAULT_OPERATOR_ID)
    created = await character_service.create_character(
        CreateCharacterRequest(
            name="Mio",
            summary="A singer with a planned two-season arc.",
            arc_template_id="pilot_arc",
            arc_series_id="starlight_series",
        ),
        user_id=DEFAULT_OPERATOR_ID,
    )

    exported = await service.export(created.id, user_id=DEFAULT_OPERATOR_ID)
    unpacked = unpack_character_card(exported.blob)

    manifest = unpacked.manifest
    assert manifest["character"]["arc_series_ref"] == "starlight_series"
    assert manifest["bundled_arc_templates"] == ["pilot_arc", "finale_arc"]
    assert set(unpacked.arc_templates) == {"pilot_arc.yaml", "finale_arc.yaml"}
    assert len(manifest["bundled_arc_series"]) == 1
    bundled = manifest["bundled_arc_series"][0]
    assert bundled["id"] == "starlight_series"
    assert bundled["title"] == "Starlight Series"
    assert bundled["members"] == [
        {"template_ref": "pilot_arc", "position": 0},
        {"template_ref": "finale_arc", "position": 1},
    ]

