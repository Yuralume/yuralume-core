from __future__ import annotations

from pathlib import Path

import pytest

from kokoro_link.application.dto.character_card import (
    CHARACTER_CARD_SCHEMA_VERSION,
    CharacterCardArcSeriesBundle,
    CharacterCardArcSeriesMember,
    CharacterCardManifest,
    CharacterCardMeta,
    CharacterCardProfile,
)
from kokoro_link.application.services.character_card_import_service import (
    CharacterCardImportService,
)
from kokoro_link.application.services.character_card_pack_service import (
    CharacterCardPackService,
)
from kokoro_link.application.services.character_image_service import (
    CharacterImageService,
)
from kokoro_link.application.services.character_service import CharacterService
from kokoro_link.domain.entities.arc_template import ArcTemplate, ArcTemplateBeat
from kokoro_link.infrastructure.character_card.arc_template_yaml import (
    dump_arc_template_to_yaml,
)
from kokoro_link.infrastructure.character_card.pack_catalog import (
    CharacterCardPackCatalog,
)
from kokoro_link.infrastructure.character_card.packager import pack_character_card
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


def _write_series_pack(directory: Path) -> None:
    templates = [
        _template("pilot_arc", "Pilot Arc"),
        _template("finale_arc", "Finale Arc"),
    ]
    manifest = CharacterCardManifest(
        schema_version=CHARACTER_CARD_SCHEMA_VERSION,
        card=CharacterCardMeta(
            title="Mio Series Pack",
            author="Tester",
            description="Character plus fixed continuation series.",
        ),
        character=CharacterCardProfile(
            name="Mio",
            summary="A singer with a planned two-season arc.",
            arc_template_ref="pilot_arc",
            arc_series_ref="starlight_series",
        ),
        bundled_arc_templates=["pilot_arc", "finale_arc"],
        bundled_arc_series=[
            CharacterCardArcSeriesBundle(
                id="starlight_series",
                title="Starlight Series",
                premise="A fixed two-season journey.",
                members=[
                    CharacterCardArcSeriesMember(
                        template_ref="pilot_arc",
                        position=0,
                    ),
                    CharacterCardArcSeriesMember(
                        template_ref="finale_arc",
                        position=1,
                    ),
                ],
            ),
        ],
    )
    blob = pack_character_card(
        manifest_json=manifest.model_dump_json(indent=2),
        stage_images=[],
        arc_templates=[
            (f"{template.id}.yaml", dump_arc_template_to_yaml(template))
            for template in templates
        ],
    )
    (directory / "mio_series.lumecard").write_bytes(blob)


def _build_service(directory: Path) -> tuple[
    CharacterCardPackService,
    InMemoryArcSeriesRepository,
]:
    char_repo = InMemoryCharacterRepository()
    character_service = CharacterService(char_repo)
    image_service = CharacterImageService(
        character_repository=char_repo,
        uploads_dir=Path("."),
        object_storage=InMemoryObjectStorage(public_base_url="/uploads"),
    )
    template_repo = InMemoryArcTemplateRepository()
    series_repo = InMemoryArcSeriesRepository()
    import_service = CharacterCardImportService(
        character_service=character_service,
        character_image_service=image_service,
        arc_template_repository=template_repo,
        arc_series_repository=series_repo,
    )
    return (
        CharacterCardPackService(
            catalog=CharacterCardPackCatalog(directories=[directory]),
            import_service=import_service,
        ),
        series_repo,
    )


def test_list_available_projects_bundled_series_summary(tmp_path: Path) -> None:
    _write_series_pack(tmp_path)
    service, _series_repo = _build_service(tmp_path)

    [summary] = service.list_available()

    assert summary.pack_id == "mio_series"
    assert summary.has_arc_series is True
    assert summary.bundled_arc_series_count == 1
    assert summary.bundled_arc_series_titles == ["Starlight Series"]
    assert summary.bundled_arc_series_member_count == 2


@pytest.mark.asyncio
async def test_install_pack_lands_bundled_series(tmp_path: Path) -> None:
    _write_series_pack(tmp_path)
    service, series_repo = _build_service(tmp_path)

    result = await service.install("mio_series", user_id="installer")

    assert result.landed_arc_series_ids == ["starlight_series"]
    assert result.character.arc_series_id == "starlight_series"
    landed = await series_repo.get_for_user("starlight_series", user_id="installer")
    assert landed is not None
    assert landed.member_template_ids == ("pilot_arc", "finale_arc")

