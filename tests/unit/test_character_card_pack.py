"""TDD for the character-card marketplace (M4): catalogue + install.

A pack is just a ``.lumecard`` on disk. Listing projects its manifest
into a display summary; installing runs the same import path as a manual
upload, creating a brand-new character owned by the installer. Tests use
a temp pack directory so they don't couple to the shipped demo files.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kokoro_link.application.dto.character_card import (
    CHARACTER_CARD_SCHEMA_VERSION,
    CharacterCardManifest,
    CharacterCardMeta,
    CharacterCardProfile,
)
from kokoro_link.application.dto.character import InitialRelationshipPayload
from kokoro_link.application.services.character_card_import_service import (
    CharacterCardImportService,
)
from kokoro_link.application.services.character_card_pack_service import (
    CharacterCardPackNotFoundError,
    CharacterCardPackService,
)
from kokoro_link.application.services.character_image_service import (
    CharacterImageService,
)
from kokoro_link.application.services.character_service import CharacterService
from kokoro_link.domain.entities.arc_template import (
    ArcTemplate,
    ArcTemplateBeat,
)
from kokoro_link.infrastructure.character_card.arc_template_yaml import (
    dump_arc_template_to_yaml,
)
from kokoro_link.infrastructure.character_card.pack_catalog import (
    CharacterCardPackCatalog,
)
from kokoro_link.infrastructure.character_card.packager import (
    pack_character_card,
    unpack_character_card,
)
from kokoro_link.infrastructure.repositories.in_memory_arc_templates import (
    InMemoryArcTemplateRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_initial_relationship import (
    InMemoryCharacterOperatorRelationshipSeedRepository,
)
from kokoro_link.infrastructure.storage.in_memory import InMemoryObjectStorage

_INSTALLER = "installer-user"
_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


class _RecordingTranslator:
    def __init__(self) -> None:
        self.calls: list[tuple[CharacterCardProfile, str]] = []

    async def translate_profile(
        self,
        profile: CharacterCardProfile,
        *,
        target_language: str,
    ) -> CharacterCardProfile:
        self.calls.append((profile, target_language))
        return profile.model_copy(
            update={
                "name": "Mio",
                "summary": "A college student working at a cafe.",
                "personality": ["bright"],
            },
        )


def _sample_template() -> ArcTemplate:
    return ArcTemplate.create(
        id="cafe_idol",
        title="咖啡廳偶像試鏡",
        premise="一段在咖啡廳打工、被捲入試鏡的兩週。",
        theme="ambition",
        duration_days=14,
        beats=[
            ArcTemplateBeat.create(
                sequence=0, day_offset=0, title="傳單",
                summary="她撿到一張試鏡傳單。", tension="setup",
            ),
        ],
    )


def _write_demo_pack(
    directory: Path,
    pack_id: str = "demo_mio",
    *,
    with_stage_image: bool = False,
) -> Path:
    template = _sample_template()
    stage_images = ["assets/stage/0.png"] if with_stage_image else []
    manifest = CharacterCardManifest(
        schema_version=CHARACTER_CARD_SCHEMA_VERSION,
        card=CharacterCardMeta(
            title="美緒 — 示範角色",
            author="Tester",
            description="示範卡",
            tags=["現代", "示範"],
            note="建議動漫風格 profile",
        ),
        character=CharacterCardProfile(
            name="美緒",
            summary="咖啡廳打工女大生",
            personality=["開朗"],
            world_frame="modern",
            proactive_enabled=True,
            arc_template_ref="cafe_idol",
        ),
        stage_images=stage_images,
        bundled_arc_templates=["cafe_idol"],
    )
    blob = pack_character_card(
        manifest_json=manifest.model_dump_json(indent=2),
        stage_images=[("assets/stage/0.png", _PNG)] if with_stage_image else [],
        arc_templates=[("cafe_idol.yaml", dump_arc_template_to_yaml(template))],
    )
    path = directory / f"{pack_id}.lumecard"
    path.write_bytes(blob)
    return path


def _build_service(directory: Path) -> tuple[
    CharacterCardPackService,
    CharacterService,
    InMemoryCharacterRepository,
    InMemoryArcTemplateRepository,
    InMemoryCharacterOperatorRelationshipSeedRepository,
]:
    return _build_service_with_translator(directory, translator=None)


def _build_service_with_translator(
    directory: Path,
    *,
    translator: _RecordingTranslator | None,
) -> tuple[
    CharacterCardPackService,
    CharacterService,
    InMemoryCharacterRepository,
    InMemoryArcTemplateRepository,
    InMemoryCharacterOperatorRelationshipSeedRepository,
]:
    char_repo = InMemoryCharacterRepository()
    relationship_repo = InMemoryCharacterOperatorRelationshipSeedRepository()
    character_service = CharacterService(
        char_repo,
        relationship_seed_repository=relationship_repo,
    )
    storage = InMemoryObjectStorage(public_base_url="/uploads")
    image_service = CharacterImageService(
        character_repository=char_repo,
        uploads_dir=Path("."),
        object_storage=storage,
    )
    arc_repo = InMemoryArcTemplateRepository()
    import_service = CharacterCardImportService(
        character_service=character_service,
        character_image_service=image_service,
        arc_template_repository=arc_repo,
        translator=translator,
    )
    catalog = CharacterCardPackCatalog(directories=[directory])
    service = CharacterCardPackService(
        catalog=catalog, import_service=import_service,
    )
    return service, character_service, char_repo, arc_repo, relationship_repo


def test_list_available_projects_manifest(tmp_path: Path) -> None:
    _write_demo_pack(tmp_path)
    service, *_ = _build_service(tmp_path)

    summaries = service.list_available()

    assert len(summaries) == 1
    summary = summaries[0]
    assert summary.pack_id == "demo_mio"
    assert summary.title == "美緒 — 示範角色"
    assert summary.author == "Tester"
    assert summary.tags == ["現代", "示範"]
    assert summary.bundled_arc_template_count == 1
    assert summary.stage_image_count == 0


def test_list_available_includes_preview_image_urls(tmp_path: Path) -> None:
    _write_demo_pack(tmp_path, with_stage_image=True)
    service, *_ = _build_service(tmp_path)

    previews = service.list_available()

    assert len(previews) == 1
    preview = previews[0]
    assert preview.image_urls == ["/api/v1/character-cards/demo_mio/images/0"]
    assert preview.stage_image_count == 1
    assert preview.has_main_arc is True
    assert preview.bundled_arc_titles == ["咖啡廳偶像試鏡"]
    assert preview.name == "美緒"
    assert preview.summary == "咖啡廳打工女大生"


@pytest.mark.asyncio
async def test_preview_pack_can_translate_current_card_without_importing(
    tmp_path: Path,
) -> None:
    _write_demo_pack(tmp_path, with_stage_image=True)
    translator = _RecordingTranslator()
    service, character_service, _char_repo, _arc_repo, _rel_repo = (
        _build_service_with_translator(tmp_path, translator=translator)
    )

    preview = await service.preview(
        "demo_mio",
        translate=True,
        target_language="en-US",
    )

    assert preview.pack_id == "demo_mio"
    assert preview.title == "Mio"
    assert preview.description == "A college student working at a cafe."
    assert preview.name == "Mio"
    assert preview.summary == "A college student working at a cafe."
    assert preview.personality == ["bright"]
    assert preview.image_urls == ["/api/v1/character-cards/demo_mio/images/0"]
    assert [call[1] for call in translator.calls] == ["en-US"]
    assert await character_service.list_characters(user_id=_INSTALLER) == []


def test_get_pack_image_returns_bytes_and_content_type(tmp_path: Path) -> None:
    _write_demo_pack(tmp_path, with_stage_image=True)
    service, *_ = _build_service(tmp_path)

    image = service.get_image("demo_mio", 0)

    assert image.data == _PNG
    assert image.content_type == "image/png"
    assert image.filename == "0.png"


def test_get_pack_image_unknown_or_out_of_range_raises_not_found(
    tmp_path: Path,
) -> None:
    _write_demo_pack(tmp_path, with_stage_image=True)
    service, *_ = _build_service(tmp_path)

    with pytest.raises(CharacterCardPackNotFoundError):
        service.get_image("ghost", 0)
    with pytest.raises(CharacterCardPackNotFoundError):
        service.get_image("demo_mio", 1)


def test_list_available_skips_unreadable_pack(tmp_path: Path) -> None:
    _write_demo_pack(tmp_path, pack_id="good")
    (tmp_path / "broken.lumecard").write_bytes(b"not a zip")
    service, *_ = _build_service(tmp_path)

    summaries = service.list_available()

    # The junk file is skipped (fail-soft), the good one survives.
    assert [s.pack_id for s in summaries] == ["good"]


@pytest.mark.asyncio
async def test_install_creates_owned_character_and_lands_arc(tmp_path: Path) -> None:
    _write_demo_pack(tmp_path)
    service, _cs, char_repo, arc_repo, _rel_repo = _build_service(tmp_path)

    result = await service.install("demo_mio", user_id=_INSTALLER)

    assert result.character.name == "美緒"
    entity = await char_repo.get(result.character.id)
    assert entity is not None
    assert entity.user_id == _INSTALLER
    # Arc template landed for the installer and bound to the character.
    assert result.landed_arc_template_ids == ["cafe_idol"]
    assert result.character.arc_template_id == "cafe_idol"
    landed = await arc_repo.get_for_user("cafe_idol", user_id=_INSTALLER)
    assert landed is not None


@pytest.mark.asyncio
async def test_install_can_seed_installer_confirmed_initial_relationship(
    tmp_path: Path,
) -> None:
    _write_demo_pack(tmp_path)
    service, _cs, _char_repo, _arc_repo, relationship_repo = _build_service(tmp_path)

    result = await service.install(
        "demo_mio",
        user_id=_INSTALLER,
        initial_relationship=InitialRelationshipPayload(
            relationship_label="想從朋友開始",
            known_context="安裝前看過這張角色卡，想慢慢熟悉。",
            user_address_name="小夏",
            character_address_name="美緒",
            tone_distance="自然但保留一點生疏",
            familiarity_boundary="只知道角色卡內容，不要假裝有共同經歷。",
            schedule_involvement_policy="mention_only",
        ),
    )

    seed = await relationship_repo.get(result.character.id, _INSTALLER)
    assert seed is not None
    assert seed.relationship_label == "想從朋友開始"
    assert seed.known_context.startswith("安裝前看過")
    assert seed.schedule_involvement_policy == "mention_only"
    assert seed.proactive_permission is False


@pytest.mark.asyncio
async def test_install_can_translate_pack_before_creating_character(
    tmp_path: Path,
) -> None:
    _write_demo_pack(tmp_path)
    translator = _RecordingTranslator()
    service, _cs, _char_repo, _arc_repo, _rel_repo = _build_service_with_translator(
        tmp_path,
        translator=translator,
    )

    result = await service.install(
        "demo_mio",
        user_id=_INSTALLER,
        translate=True,
        target_language="en-US",
    )

    assert result.character.name == "Mio"
    assert result.character.summary == "A college student working at a cafe."
    assert result.character.personality == ["bright"]
    assert [call[1] for call in translator.calls] == ["en-US"]


@pytest.mark.asyncio
async def test_install_unknown_pack_raises_not_found(tmp_path: Path) -> None:
    service, *_ = _build_service(tmp_path)
    with pytest.raises(CharacterCardPackNotFoundError):
        await service.install("ghost", user_id=_INSTALLER)


def test_shipped_demo_packs_load() -> None:
    """Every repo-shipped card must parse.

    Bundled card filenames are content, not API fixtures: maintainers
    can add, remove, or rename packs without changing this test.
    """
    catalog = CharacterCardPackCatalog()
    pack_ids = set(catalog.list_pack_files())
    assert pack_ids
    for pack_id in pack_ids:
        blob = catalog.read_blob(pack_id)
        assert blob is not None
        CharacterCardManifest.model_validate(unpack_character_card(blob).manifest)
