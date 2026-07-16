"""TDD for character-card import (M2) + the export→import round trip.

These tests model two deployments: a card is exported from one set of
repos and imported into a *fresh* set, proving the A-layer settings are
portable while B/C layers reset and bundled arc templates land (with id
collisions remapped). See ``docs/CHARACTER_CARD_PLAN.md`` §4 / §6.
"""

from __future__ import annotations

import io
import json
import zipfile
from base64 import b64decode
from dataclasses import replace
from pathlib import Path

import pytest

from kokoro_link.application.dto.character import (
    CharacterCompanionPayload,
    CharacterDispositionPayload,
    CharacterPersonalityTypePayload,
    CreateCharacterRequest,
    InitialRelationshipPayload,
    UpdateCharacterRequest,
)
from kokoro_link.application.dto.character_card import CharacterCardProfile
from kokoro_link.application.services.character_card_export_service import (
    CharacterCardExportService,
)
from kokoro_link.application.services.character_card_import_service import (
    CharacterCardImportError,
    CharacterCardImportService,
    InvalidCharacterCardError,
    UnsupportedCardSchemaError,
)
from kokoro_link.application.services.character_image_service import (
    CharacterImageService,
)
from kokoro_link.application.services.character_service import CharacterService
from kokoro_link.domain.entities.arc_template import (
    ARC_TEMPLATE_SCOPE_CHARACTER_BOUND,
    ArcTemplate,
    ArcTemplateBeat,
)
from kokoro_link.domain.entities.operator_profile import DEFAULT_OPERATOR_ID
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

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
_IMPORTER = "importer-user"


class _RecordingTranslator:
    def __init__(
        self,
        translated: CharacterCardProfile | None = None,
    ) -> None:
        self.translated = translated
        self.calls: list[tuple[CharacterCardProfile, str]] = []

    async def translate_profile(
        self,
        profile: CharacterCardProfile,
        *,
        target_language: str,
    ) -> CharacterCardProfile:
        self.calls.append((profile, target_language))
        return self.translated or profile


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
            ),
        ],
    )


async def _export_a_card(
    *,
    with_template: bool = True,
    character_bound_template: bool = False,
) -> bytes:
    """Build a card on 'deployment A' and return the blob."""
    char_repo = InMemoryCharacterRepository()
    character_service = CharacterService(char_repo)
    storage = InMemoryObjectStorage(public_base_url="/uploads")
    arc_repo = InMemoryArcTemplateRepository()
    export = CharacterCardExportService(
        character_service=character_service,
        object_storage=storage,
        arc_template_repository=arc_repo,
        app_version="test",
    )

    arc_template_id = None
    if with_template and not character_bound_template:
        template = _sample_template()
        await arc_repo.save_for_user(template, user_id=DEFAULT_OPERATOR_ID)
        arc_template_id = template.id
    stored = await storage.put_bytes(
        object_key="characters/seed/0.png", content=_PNG, content_type="image/png",
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
            arc_template_id=arc_template_id,
            world_frame="modern",
            proactive_enabled=True,
            proactive_daily_limit=5,
            companions=[CharacterCompanionPayload(name="店長", role="上司")],
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
    if with_template and character_bound_template:
        template = replace(
            _sample_template(),
            applicability_scope=ARC_TEMPLATE_SCOPE_CHARACTER_BOUND,
            target_character_ids=(created.id,),
        )
        await arc_repo.save_for_user(template, user_id=DEFAULT_OPERATOR_ID)
        await character_service.update_character(
            created.id,
            UpdateCharacterRequest(arc_template_id=template.id),
            user_id=DEFAULT_OPERATOR_ID,
        )
    exported = await export.export(created.id, user_id=DEFAULT_OPERATOR_ID)
    return exported.blob


def _build_import_side(
    *,
    translator: _RecordingTranslator | None = None,
) -> tuple[
    CharacterCardImportService,
    CharacterService,
    InMemoryCharacterRepository,
    InMemoryObjectStorage,
    InMemoryArcTemplateRepository,
    InMemoryCharacterOperatorRelationshipSeedRepository,
]:
    """A fresh 'deployment B' — empty repos and its own storage."""
    char_repo = InMemoryCharacterRepository()
    relationship_repo = InMemoryCharacterOperatorRelationshipSeedRepository()
    character_service = CharacterService(
        char_repo,
        relationship_seed_repository=relationship_repo,
    )
    storage = InMemoryObjectStorage(public_base_url="/uploads-b")
    image_service = CharacterImageService(
        character_repository=char_repo,
        uploads_dir=Path("."),
        object_storage=storage,
    )
    arc_repo = InMemoryArcTemplateRepository()
    service = CharacterCardImportService(
        character_service=character_service,
        character_image_service=image_service,
        arc_template_repository=arc_repo,
        translator=translator,
    )
    return service, character_service, char_repo, storage, arc_repo, relationship_repo


# --- round trip --------------------------------------------------------


@pytest.mark.asyncio
async def test_round_trip_preserves_a_layer_and_resets_runtime() -> None:
    blob = await _export_a_card()
    service, character_service, char_repo, storage, _arc_repo, _rel_repo = (
        _build_import_side()
    )

    result = await service.import_card(blob, user_id=_IMPORTER)
    char = result.character

    # A-layer carried across.
    assert char.name == "Mio"
    assert char.personality == ["溫柔", "好奇"]
    assert char.boundaries == ["不聊政治"]
    assert char.gender_identity == "女性"
    assert char.third_person_pronoun == "她"
    assert char.visual_gender_presentation == "feminine college student"
    assert char.visual_subject_type == "human"
    assert char.visual_generation_style == "realistic"
    assert char.proactive_enabled is True
    assert char.proactive_daily_limit == 5
    assert char.disposition.candor == "high"
    assert char.personality_type.code == "ISFJ"
    assert char.personality_type.rationale == "溫和、照顧人，重視穩定關係。"
    assert char.companions[0].name == "店長"

    # Owned by the importer, brand-new id, fresh runtime.
    entity = await char_repo.get(char.id)
    assert entity is not None
    assert entity.user_id == _IMPORTER

    # Stage image re-uploaded into the importer's own storage.
    assert len(char.image_urls) == 1
    assert char.image_urls[0].startswith("/uploads-b/")
    object_key = storage.object_key_from_url(char.image_urls[0])
    assert object_key is not None
    assert await storage.get_bytes(object_key=object_key) == _PNG


@pytest.mark.asyncio
async def test_import_can_seed_importer_confirmed_initial_relationship() -> None:
    blob = await _export_a_card()
    service, _cs, _cr, _st, _arc_repo, relationship_repo = _build_import_side()

    result = await service.import_card(
        blob,
        user_id=_IMPORTER,
        initial_relationship=InitialRelationshipPayload(
            relationship_label="先前在社群聊過的朋友",
            known_context="匯入者確認曾在官方社群看過她的創作設定。",
            user_address_name="小夏",
            character_address_name="Mio",
            tone_distance="友善但不要裝熟",
            familiarity_boundary="可以知道匯入者看過角色卡，不可杜撰共同生活回憶。",
            schedule_involvement_policy="invite_required",
            proactive_permission=True,
            proactive_cadence_hint="先一週一兩次短訊息。",
            user_profile_notes="匯入者喜歡咖啡廳題材。",
        ),
    )

    seed = await relationship_repo.get(result.character.id, _IMPORTER)
    assert seed is not None
    assert seed.relationship_label == "先前在社群聊過的朋友"
    assert seed.user_address_name == "小夏"
    assert seed.character_address_name == "Mio"
    assert seed.schedule_involvement_policy == "invite_required"
    assert seed.proactive_permission is True
    assert "不可杜撰" in seed.familiarity_boundary


@pytest.mark.asyncio
async def test_import_lands_bundled_arc_template_and_binds_it() -> None:
    blob = await _export_a_card()
    service, _cs, _cr, _st, arc_repo, _rel_repo = _build_import_side()

    result = await service.import_card(blob, user_id=_IMPORTER)

    assert result.landed_arc_template_ids == ["cafe_idol"]
    assert result.character.arc_template_id == "cafe_idol"
    landed = await arc_repo.get_for_user("cafe_idol", user_id=_IMPORTER)
    assert landed is not None
    assert landed.title == "咖啡廳偶像試鏡"


@pytest.mark.asyncio
async def test_import_remaps_self_ref_to_new_character_id() -> None:
    blob = await _export_a_card(character_bound_template=True)
    service, _cs, _cr, _st, arc_repo, _rel_repo = _build_import_side()

    result = await service.import_card(blob, user_id=_IMPORTER)

    landed = await arc_repo.get_for_user("cafe_idol", user_id=_IMPORTER)
    assert landed is not None
    assert landed.applicability_scope == ARC_TEMPLATE_SCOPE_CHARACTER_BOUND
    assert landed.target_character_ids == (result.character.id,)
    assert landed.target_character_refs == ()
    assert result.character.arc_template_id == "cafe_idol"


@pytest.mark.asyncio
async def test_import_remaps_colliding_arc_template_id() -> None:
    blob = await _export_a_card()
    service, _cs, _cr, _st, arc_repo, _rel_repo = _build_import_side()
    # Importer already owns a *different* template under the same slug.
    pre_existing = replace(
        _sample_template(), title="我自己的咖啡廳故事",
    )
    await arc_repo.save_for_user(pre_existing, user_id=_IMPORTER)

    result = await service.import_card(blob, user_id=_IMPORTER)

    landed_id = result.landed_arc_template_ids[0]
    assert landed_id != "cafe_idol"
    assert landed_id.startswith("cafe_idol-")
    # The character binds to the remapped id, not the colliding original.
    assert result.character.arc_template_id == landed_id
    # The importer's pre-existing template is untouched.
    mine = await arc_repo.get_for_user("cafe_idol", user_id=_IMPORTER)
    assert mine is not None
    assert mine.title == "我自己的咖啡廳故事"
    # The bundled one landed under the new id with its own content.
    landed = await arc_repo.get_for_user(landed_id, user_id=_IMPORTER)
    assert landed is not None
    assert landed.title == "咖啡廳偶像試鏡"


@pytest.mark.asyncio
async def test_import_without_template_binds_nothing() -> None:
    blob = await _export_a_card(with_template=False)
    service, _cs, _cr, _st, _arc_repo, _rel_repo = _build_import_side()

    result = await service.import_card(blob, user_id=_IMPORTER)

    assert result.landed_arc_template_ids == []
    assert result.character.arc_template_id is None


@pytest.mark.asyncio
async def test_preview_card_projects_a_layer_without_creating_character() -> None:
    blob = await _export_a_card()
    service, _character_service, char_repo, _storage, _arc_repo, _rel_repo = (
        _build_import_side()
    )

    preview = await service.preview_card(blob)

    assert preview.title == "Mio"
    assert preview.name == "Mio"
    assert preview.summary == "咖啡廳打工的女大生"
    assert preview.personality == ["溫柔", "好奇"]
    assert preview.interests == ["咖啡", "唱歌"]
    assert preview.boundaries == ["不聊政治"]
    assert preview.gender_identity == "女性"
    assert preview.third_person_pronoun == "她"
    assert preview.visual_gender_presentation == "feminine college student"
    assert preview.visual_subject_type == "human"
    assert preview.visual_generation_style == "realistic"
    assert preview.disposition.candor == "high"
    assert preview.companions[0].name == "店長"
    assert preview.companions[0].role == "上司"
    assert preview.has_main_arc is True
    assert preview.bundled_arc_template_count == 1
    assert preview.bundled_arc_titles == ["咖啡廳偶像試鏡"]
    assert preview.stage_image_count == 1
    assert len(preview.image_urls) == 1
    prefix, encoded = preview.image_urls[0].split(",", 1)
    assert prefix == "data:image/png;base64"
    assert b64decode(encoded) == _PNG
    assert await char_repo.list() == []


@pytest.mark.asyncio
async def test_import_translate_true_uses_translated_profile_without_touching_structure() -> None:
    blob = await _export_a_card()
    translated = CharacterCardProfile(
        name="Mio",
        summary="A college student working at a cafe.",
        personality=["gentle", "curious"],
        interests=["coffee", "singing"],
        boundaries=["avoids politics"],
        gender_identity="woman",
        third_person_pronoun="she",
        visual_gender_presentation="feminine college student",
        visual_subject_type="human",
        visual_generation_style="realistic",
        arc_template_ref="cafe_idol",
        world_frame="modern",
        proactive_enabled=True,
        proactive_daily_limit=5,
        companions=[CharacterCompanionPayload(name="Manager", role="supervisor")],
        disposition=CharacterDispositionPayload(candor="high"),
    )
    translator = _RecordingTranslator(translated)
    service, _character_service, _char_repo, _storage, _arc_repo, _rel_repo = (
        _build_import_side(translator=translator)
    )

    result = await service.import_card(
        blob,
        user_id=_IMPORTER,
        translate=True,
        target_language="en-US",
    )

    assert [call[1] for call in translator.calls] == ["en-US"]
    assert result.character.name == "Mio"
    assert result.character.summary == "A college student working at a cafe."
    assert result.character.personality == ["gentle", "curious"]
    assert result.character.interests == ["coffee", "singing"]
    assert result.character.boundaries == ["avoids politics"]
    assert result.character.gender_identity == "woman"
    assert result.character.third_person_pronoun == "she"
    assert result.character.visual_gender_presentation == "feminine college student"
    assert result.character.visual_subject_type == "human"
    assert result.character.visual_generation_style == "realistic"
    assert result.character.disposition.candor == "high"
    assert result.character.proactive_daily_limit == 5
    assert result.character.arc_template_id == "cafe_idol"
    assert result.character.companions[0].name == "Manager"


@pytest.mark.asyncio
async def test_import_translate_false_does_not_call_translator() -> None:
    blob = await _export_a_card()
    translator = _RecordingTranslator()
    service, *_ = _build_import_side(translator=translator)

    result = await service.import_card(
        blob,
        user_id=_IMPORTER,
        translate=False,
        target_language="en-US",
    )

    assert result.character.name == "Mio"
    assert translator.calls == []


@pytest.mark.asyncio
async def test_import_translate_true_without_target_language_is_noop() -> None:
    blob = await _export_a_card()
    translator = _RecordingTranslator()
    service, *_ = _build_import_side(translator=translator)

    result = await service.import_card(
        blob,
        user_id=_IMPORTER,
        translate=True,
        target_language="",
    )

    assert result.character.name == "Mio"
    assert translator.calls == []


@pytest.mark.asyncio
async def test_preview_translate_true_uses_translated_profile_without_creating_character() -> None:
    blob = await _export_a_card()
    translated = CharacterCardProfile(
        name="Mio",
        summary="A college student working at a cafe.",
        personality=["gentle"],
        interests=["coffee"],
        arc_template_ref="cafe_idol",
    )
    translator = _RecordingTranslator(translated)
    service, _character_service, char_repo, _storage, _arc_repo, _rel_repo = (
        _build_import_side(translator=translator)
    )

    preview = await service.preview_card(
        blob,
        translate=True,
        target_language="en-US",
    )

    assert [call[1] for call in translator.calls] == ["en-US"]
    assert preview.title == "Mio"
    assert preview.description == "A college student working at a cafe."
    assert preview.name == "Mio"
    assert preview.summary == "A college student working at a cafe."
    assert preview.personality == ["gentle"]
    assert preview.has_main_arc is True
    assert await char_repo.list() == []


# --- rejection paths ---------------------------------------------------


@pytest.mark.asyncio
async def test_import_rejects_unsupported_schema_version() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr(
            "manifest.json",
            json.dumps({"schema_version": 999, "character": {"name": "X"}}),
        )
    service, *_ = _build_import_side()
    with pytest.raises(UnsupportedCardSchemaError):
        await service.import_card(buffer.getvalue(), user_id=_IMPORTER)


@pytest.mark.asyncio
async def test_preview_rejects_unsupported_schema_version() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr(
            "manifest.json",
            json.dumps({"schema_version": 999, "character": {"name": "X"}}),
        )
    service, *_ = _build_import_side()
    with pytest.raises(UnsupportedCardSchemaError):
        await service.preview_card(buffer.getvalue())


@pytest.mark.asyncio
async def test_import_rejects_manifest_missing_character() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("manifest.json", json.dumps({"schema_version": 1}))
    service, *_ = _build_import_side()
    with pytest.raises(CharacterCardImportError):
        await service.import_card(buffer.getvalue(), user_id=_IMPORTER)


@pytest.mark.asyncio
async def test_import_rejects_non_zip() -> None:
    service, *_ = _build_import_side()
    with pytest.raises(InvalidCharacterCardError):
        await service.import_card(b"not a zip", user_id=_IMPORTER)
