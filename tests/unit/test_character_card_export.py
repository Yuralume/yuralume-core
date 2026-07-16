"""BDD/TDD for character-card export (M1) + the zip packager.

A ``.lumecard`` bundles a character's portable A-layer settings plus
optional arc templates. These tests lock the export projection (A in,
B/C out), the stage-image round-trip through object storage, and the
zip packager's safety guards.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from kokoro_link.application.dto.character import (
    CharacterCompanionPayload,
    CharacterDispositionPayload,
    CharacterPersonalityTypePayload,
    CreateCharacterRequest,
    UpdateCharacterRequest,
)
from kokoro_link.application.dto.character_card import (
    CHARACTER_CARD_SCHEMA_VERSION,
)
from kokoro_link.application.services.character_card_export_service import (
    CharacterCardExportService,
    CharacterCardNotFoundError,
)
from kokoro_link.application.services.character_service import CharacterService
from kokoro_link.domain.entities.arc_template import (
    ARC_TEMPLATE_CHARACTER_REF_SELF,
    ARC_TEMPLATE_SCOPE_CHARACTER_BOUND,
    ArcTemplate,
    ArcTemplateBeat,
)
from kokoro_link.domain.entities.operator_profile import DEFAULT_OPERATOR_ID
from kokoro_link.infrastructure.character_card.arc_template_yaml import (
    dump_arc_template_to_yaml,
    load_arc_template_from_yaml,
)
from kokoro_link.infrastructure.character_card.packager import (
    InvalidCharacterCardError,
    pack_character_card,
    unpack_character_card,
)
from kokoro_link.infrastructure.repositories.in_memory_arc_templates import (
    InMemoryArcTemplateRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)
from kokoro_link.infrastructure.storage.in_memory import InMemoryObjectStorage

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


def _sample_template(template_id: str = "cafe_idol") -> ArcTemplate:
    return ArcTemplate.create(
        id=template_id,
        title="咖啡廳偶像試鏡",
        premise="一段在咖啡廳打工的日常，逐漸被捲入一場偶像試鏡的兩週。",
        theme="ambition",
        tone="lighthearted",
        duration_days=14,
        beats=[
            ArcTemplateBeat.create(
                sequence=0,
                day_offset=0,
                title="傳單",
                summary="她在店裡撿到一張試鏡傳單，心動了一下。",
                tension="setup",
                scene_type="encounter",
                location="咖啡廳",
                scene_characters=["店長"],
                dramatic_question="要不要報名？",
            ),
            ArcTemplateBeat.create(
                sequence=1,
                day_offset=7,
                title="初選",
                summary="第一輪試鏡，她緊張到忘詞，卻意外被評審記住。",
                tension="rising",
            ),
        ],
    )


async def _build() -> tuple[
    CharacterCardExportService,
    CharacterService,
    InMemoryObjectStorage,
    InMemoryArcTemplateRepository,
]:
    char_repo = InMemoryCharacterRepository()
    character_service = CharacterService(char_repo)
    storage = InMemoryObjectStorage(public_base_url="/uploads")
    arc_repo = InMemoryArcTemplateRepository()
    service = CharacterCardExportService(
        character_service=character_service,
        object_storage=storage,
        arc_template_repository=arc_repo,
        app_version="test",
    )
    return service, character_service, storage, arc_repo


async def _seed_full_character(
    character_service: CharacterService,
    storage: InMemoryObjectStorage,
    arc_repo: InMemoryArcTemplateRepository,
) -> str:
    template = _sample_template()
    await arc_repo.save_for_user(template, user_id=DEFAULT_OPERATOR_ID)
    stored = await storage.put_bytes(
        object_key="characters/seed/0.png",
        content=_PNG,
        content_type="image/png",
    )
    created = await character_service.create_character(
        CreateCharacterRequest(
            name="Mio",
            summary="咖啡廳打工的女大生",
            personality=["溫柔", "好奇"],
            interests=["咖啡", "唱歌"],
            boundaries=["不聊政治"],
            gender_identity="女性",
            third_person_pronoun="她",
            visual_gender_presentation="feminine college student",
            visual_subject_type="human",
            visual_generation_style="realistic",
            image_urls=[stored.url],
            arc_template_id=template.id,
            world_frame="modern",
            proactive_enabled=True,
            companions=[
                CharacterCompanionPayload(name="店長", role="上司"),
            ],
            disposition=CharacterDispositionPayload(candor="high"),
            personality_type=CharacterPersonalityTypePayload(
                code="ISFJ",
                source="user_explicit",
                confidence=0.9,
                rationale="溫和、照顧人，重視穩定關係。",
                consistency_notes=["不要把類型當硬規則。"],
            ),
        ),
        user_id=DEFAULT_OPERATOR_ID,
    )
    return created.id


# --- packager ----------------------------------------------------------


def test_packager_round_trips_manifest_assets_and_templates() -> None:
    blob = pack_character_card(
        manifest_json='{"schema_version": 1, "hello": "world"}',
        stage_images=[("assets/stage/0.png", _PNG)],
        arc_templates=[("cafe.yaml", "id: cafe\n")],
    )
    unpacked = unpack_character_card(blob)
    assert unpacked.manifest["schema_version"] == 1
    assert unpacked.manifest["hello"] == "world"
    assert unpacked.stage_images["assets/stage/0.png"] == _PNG
    assert unpacked.arc_templates["cafe.yaml"] == "id: cafe\n"


def test_unpack_rejects_non_zip() -> None:
    with pytest.raises(InvalidCharacterCardError):
        unpack_character_card(b"not a zip at all")


def test_unpack_rejects_missing_manifest() -> None:
    blob = pack_character_card(
        manifest_json="", stage_images=[], arc_templates=[],
    )
    # manifest written but empty string is invalid JSON
    with pytest.raises(InvalidCharacterCardError):
        unpack_character_card(blob)


def test_unpack_rejects_zip_slip_member() -> None:
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("manifest.json", '{"schema_version": 1}')
        zf.writestr("../../evil.txt", "pwned")
    with pytest.raises(InvalidCharacterCardError):
        unpack_character_card(buffer.getvalue())


# --- export service ----------------------------------------------------


@pytest.mark.asyncio
async def test_export_bundles_a_layer_and_strips_runtime() -> None:
    service, character_service, storage, arc_repo = await _build()
    character_id = await _seed_full_character(
        character_service, storage, arc_repo,
    )

    exported = await service.export(character_id, user_id=DEFAULT_OPERATOR_ID)
    assert exported.filename.endswith(".lumecard")

    unpacked = unpack_character_card(exported.blob)
    manifest = unpacked.manifest
    assert manifest["schema_version"] == CHARACTER_CARD_SCHEMA_VERSION

    char = manifest["character"]
    assert char["name"] == "Mio"
    assert char["personality"] == ["溫柔", "好奇"]
    assert char["gender_identity"] == "女性"
    assert char["third_person_pronoun"] == "她"
    assert char["visual_gender_presentation"] == "feminine college student"
    assert char["visual_subject_type"] == "human"
    assert char["visual_generation_style"] == "realistic"
    assert char["proactive_enabled"] is True
    assert char["disposition"]["candor"] == "high"
    assert char["personality_type"]["code"] == "ISFJ"
    assert char["personality_type"]["rationale"] == "溫和、照顧人，重視穩定關係。"
    assert char["companions"][0]["name"] == "店長"
    # A-layer only: no state / runtime / B-layer keys leaked.
    assert "state" not in char
    assert "voice_profile" not in char
    assert "feature_models" not in char
    assert "loras" not in char
    assert "image_urls" not in char  # carried as bundled assets instead


@pytest.mark.asyncio
async def test_export_bundles_bound_arc_template_and_sets_ref() -> None:
    service, character_service, storage, arc_repo = await _build()
    character_id = await _seed_full_character(
        character_service, storage, arc_repo,
    )

    unpacked = unpack_character_card(
        (await service.export(character_id, user_id=DEFAULT_OPERATOR_ID)).blob,
    )
    assert unpacked.manifest["bundled_arc_templates"] == ["cafe_idol"]
    assert unpacked.manifest["character"]["arc_template_ref"] == "cafe_idol"
    assert "cafe_idol.yaml" in unpacked.arc_templates

    # The bundled YAML re-parses into an identical template.
    reparsed = load_arc_template_from_yaml(
        unpacked.arc_templates["cafe_idol.yaml"], fallback_id="cafe_idol",
    )
    original = _sample_template()
    assert reparsed.id == original.id
    assert reparsed.title == original.title
    assert reparsed.beat_count == original.beat_count
    assert reparsed.beats[0].title == "傳單"
    assert reparsed.beats[1].day_offset == 7


@pytest.mark.asyncio
async def test_export_round_trips_stage_image_bytes() -> None:
    service, character_service, storage, arc_repo = await _build()
    character_id = await _seed_full_character(
        character_service, storage, arc_repo,
    )

    unpacked = unpack_character_card(
        (await service.export(character_id, user_id=DEFAULT_OPERATOR_ID)).blob,
    )
    assert unpacked.manifest["stage_images"] == ["assets/stage/0.png"]
    assert unpacked.stage_images["assets/stage/0.png"] == _PNG


@pytest.mark.asyncio
async def test_export_includes_extra_requested_templates() -> None:
    service, character_service, storage, arc_repo = await _build()
    character_id = await _seed_full_character(
        character_service, storage, arc_repo,
    )
    extra = _sample_template("summer_side_story")
    await arc_repo.save_for_user(extra, user_id=DEFAULT_OPERATOR_ID)

    unpacked = unpack_character_card(
        (
            await service.export(
                character_id,
                user_id=DEFAULT_OPERATOR_ID,
                include_arc_template_ids=["summer_side_story"],
            )
        ).blob,
    )
    assert set(unpacked.manifest["bundled_arc_templates"]) == {
        "cafe_idol", "summer_side_story",
    }


@pytest.mark.asyncio
async def test_export_rewrites_character_targets_to_self_ref() -> None:
    service, character_service, storage, arc_repo = await _build()
    stored = await storage.put_bytes(
        object_key="characters/seed/0.png",
        content=_PNG,
        content_type="image/png",
    )
    created = await character_service.create_character(
        CreateCharacterRequest(
            name="Mio",
            summary="咖啡廳打工的女大生",
            image_urls=[stored.url],
        ),
        user_id=DEFAULT_OPERATOR_ID,
    )
    await arc_repo.save_for_user(
        replace(
            _sample_template(),
            applicability_scope=ARC_TEMPLATE_SCOPE_CHARACTER_BOUND,
            target_character_ids=(created.id,),
        ),
        user_id=DEFAULT_OPERATOR_ID,
    )
    await character_service.update_character(
        created.id,
        UpdateCharacterRequest(arc_template_id="cafe_idol"),
        user_id=DEFAULT_OPERATOR_ID,
    )

    unpacked = unpack_character_card(
        (await service.export(created.id, user_id=DEFAULT_OPERATOR_ID)).blob,
    )
    yaml_text = unpacked.arc_templates["cafe_idol.yaml"]
    loaded = load_arc_template_from_yaml(yaml_text, fallback_id="cafe_idol")

    assert created.id not in yaml_text
    assert loaded.target_character_ids == ()
    assert loaded.target_character_refs == (ARC_TEMPLATE_CHARACTER_REF_SELF,)


@pytest.mark.asyncio
async def test_export_unknown_character_raises_not_found() -> None:
    service, *_ = await _build()
    with pytest.raises(CharacterCardNotFoundError):
        await service.export("ghost", user_id=DEFAULT_OPERATOR_ID)


@pytest.mark.asyncio
async def test_export_cross_user_is_not_found() -> None:
    service, character_service, storage, arc_repo = await _build()
    character_id = await _seed_full_character(
        character_service, storage, arc_repo,
    )
    with pytest.raises(CharacterCardNotFoundError):
        await service.export(character_id, user_id="someone-else")


# ---------- Phase A0: language round-trips through .lumecard YAML -------


def test_dump_and_load_arc_template_preserves_language() -> None:
    """Phase A0 regression guard: exporting a self-authored en-US
    template to .lumecard YAML and reimporting it must not silently
    drop back to the zh-TW domain default. ``arc_template_to_mapping``
    has to actually emit the ``language`` key for this round-trip to
    hold — the import side already reads it."""
    template = replace(_sample_template(), language="en-US")

    yaml_text = dump_arc_template_to_yaml(template)
    reloaded = load_arc_template_from_yaml(yaml_text, fallback_id=template.id)

    assert reloaded.language == "en-US"


def test_dump_omits_nothing_and_default_language_round_trips_too() -> None:
    """Sanity check the other direction: a pack-default zh-TW template
    still round-trips to zh-TW (no accidental language drift for the
    common case)."""
    template = _sample_template()
    assert template.language == "zh-TW"

    yaml_text = dump_arc_template_to_yaml(template)
    reloaded = load_arc_template_from_yaml(yaml_text, fallback_id=template.id)

    assert reloaded.language == "zh-TW"
