"""`.lumecard` bundled arc-template translate gap (SHIPPED_CONTENT_LOCALIZATION #2).

Before this change ``translate=true`` only touched ``manifest.character``;
a bundled arc template landed verbatim. These tests assert the import
path now also localizes bundled templates when translate is requested,
per-template fail-soft, and leaves them untouched when it is not.
"""

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
from kokoro_link.domain.entities.arc_template import (
    ArcTemplate,
    ArcTemplateBeat,
)
from kokoro_link.infrastructure.repositories.in_memory_arc_templates import (
    InMemoryArcTemplateRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)
from kokoro_link.infrastructure.storage.in_memory import InMemoryObjectStorage

DEFAULT_OPERATOR_ID = "default"
_IMPORTER = "importer-user"


class _RecordingArcTranslator:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[str] = []
        self._fail = fail

    async def translate_template(self, template, *, target_language):
        self.calls.append(template.id)
        if self._fail:
            raise RuntimeError("translator down")
        beats = [
            ArcTemplateBeat.create(
                sequence=b.sequence, day_offset=b.day_offset,
                title=f"EN::{b.title}", summary=f"EN::{b.summary}",
                tension=b.tension, scene_type=b.scene_type,
                location=b.location, scene_characters=list(b.scene_characters),
                dramatic_question=b.dramatic_question, required=b.required,
            )
            for b in template.beats
        ]
        return ArcTemplate.create(
            id=template.id, title=f"EN::{template.title}",
            premise=f"EN::{template.premise}", theme=template.theme,
            tone=template.tone, language=target_language,
            duration_days=template.duration_days, beats=beats,
        ).with_language(target_language)


def _sample_template() -> ArcTemplate:
    return ArcTemplate.create(
        id="cafe_idol",
        title="咖啡廳偶像試鏡",
        premise="一段在咖啡廳打工的日常。",
        theme="ambition", tone="lighthearted", language="zh-TW",
        duration_days=14,
        beats=[
            ArcTemplateBeat.create(
                sequence=0, day_offset=0, title="傳單",
                summary="她撿到一張試鏡傳單。", tension="setup",
            ),
        ],
    )


async def _export_card_with_template() -> bytes:
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
    template = _sample_template()
    await arc_repo.save_for_user(template, user_id=DEFAULT_OPERATOR_ID)
    created = await character_service.create_character(
        CreateCharacterRequest(
            name="Mio", summary="咖啡廳打工", personality=["溫柔"],
            interests=["咖啡"], boundaries=[], arc_template_id=template.id,
        ),
        user_id=DEFAULT_OPERATOR_ID,
    )
    assert created.arc_template_id == template.id
    exported = await export.export(created.id, user_id=DEFAULT_OPERATOR_ID)
    return exported.blob


def _import_side(*, arc_translator=None):
    char_repo = InMemoryCharacterRepository()
    character_service = CharacterService(char_repo)
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
        arc_template_translator=arc_translator,
    )
    return service, arc_repo


@pytest.mark.asyncio
async def test_translate_true_localizes_bundled_template() -> None:
    blob = await _export_card_with_template()
    translator = _RecordingArcTranslator()
    service, arc_repo = _import_side(arc_translator=translator)

    result = await service.import_card(
        blob, user_id=_IMPORTER, translate=True, target_language="en-US",
    )

    assert translator.calls == ["cafe_idol"]
    assert result.landed_arc_template_ids
    landed = await arc_repo.get_for_user(
        result.landed_arc_template_ids[0], user_id=_IMPORTER,
    )
    assert landed is not None
    assert landed.title == "EN::咖啡廳偶像試鏡"
    assert landed.beats[0].title == "EN::傳單"
    assert landed.language == "en-US"


@pytest.mark.asyncio
async def test_translate_false_leaves_bundled_template() -> None:
    blob = await _export_card_with_template()
    translator = _RecordingArcTranslator()
    service, arc_repo = _import_side(arc_translator=translator)

    result = await service.import_card(blob, user_id=_IMPORTER)

    assert translator.calls == []
    landed = await arc_repo.get_for_user(
        result.landed_arc_template_ids[0], user_id=_IMPORTER,
    )
    assert landed is not None
    assert landed.title == "咖啡廳偶像試鏡"


@pytest.mark.asyncio
async def test_translator_failure_is_per_template_failsoft() -> None:
    blob = await _export_card_with_template()
    translator = _RecordingArcTranslator(fail=True)
    service, arc_repo = _import_side(arc_translator=translator)

    # Import still succeeds; the template lands with authored prose.
    result = await service.import_card(
        blob, user_id=_IMPORTER, translate=True, target_language="en-US",
    )
    assert translator.calls == ["cafe_idol"]
    landed = await arc_repo.get_for_user(
        result.landed_arc_template_ids[0], user_id=_IMPORTER,
    )
    assert landed is not None
    assert landed.title == "咖啡廳偶像試鏡"
