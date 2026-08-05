"""Official cards in the card shelf: browse, preview, install.

The catalog is faked at the port, so these tests are about the decisions
Core makes with what Cloud says — which language to ask for, what an outage
looks like to a player, and the one that costs money: whether the LLM
translator gets called when an approved translation already exists.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kokoro_link.application.dto.character_card import (
    CharacterCardManifest,
    CharacterCardMeta,
    CharacterCardProfile,
)
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
from kokoro_link.application.services.official_card_pack_source import (
    OfficialCardPackSource,
    OfficialCardUnavailableError,
)
from kokoro_link.contracts.official_card_catalog import (
    OfficialCardCatalogPort,
    OfficialCardDetail,
    OfficialCardImage,
    OfficialCardNotFound,
    OfficialCardSummary,
)
from kokoro_link.domain.entities.arc_template import ArcTemplate, ArcTemplateBeat
from kokoro_link.infrastructure.character_card.arc_template_yaml import (
    dump_arc_template_to_yaml,
)
from kokoro_link.infrastructure.character_card.pack_catalog import (
    CharacterCardPackCatalog,
)
from kokoro_link.infrastructure.character_card.packager import pack_character_card
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
_CARD_ID = "mio-cafe"
_CLOUD_REF = "cloud:mio-cafe"


class _RecordingTranslator:
    """Stands in for the paid LLM translator; every call is a charge."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def translate_profile(
        self,
        profile: CharacterCardProfile,
        *,
        target_language: str,
    ) -> CharacterCardProfile:
        self.calls.append(target_language)
        return profile.model_copy(update={"name": "LLM Mio"})


class _RecordingArcTranslator:
    """The card's *other* paid translator — bundled arc-template prose."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def translate_template(
        self, template: ArcTemplate, *, target_language: str,
    ) -> ArcTemplate:
        self.calls.append(template.id)
        return template


class _FakeCatalog(OfficialCardCatalogPort):
    def __init__(
        self,
        *,
        cards: list[OfficialCardSummary] | None = None,
        detail: OfficialCardDetail | None = None,
        artifact: bytes | None = None,
    ) -> None:
        self._cards = cards
        self._detail = detail
        self._artifact = artifact
        self.missing = False
        self.requested_locales: list[str] = []
        # Whether each detail read asked to skip the cache. The install path
        # must; the browse paths must not.
        self.detail_freshness: list[bool] = []

    async def list_cards(self, *, locale: str) -> list[OfficialCardSummary] | None:
        self.requested_locales.append(locale)
        return None if self._cards is None else list(self._cards)

    async def get_card(
        self, card_id: str, *, locale: str, fresh: bool = False,
    ) -> OfficialCardDetail | None:
        self.requested_locales.append(locale)
        self.detail_freshness.append(fresh)
        if self.missing:
            raise OfficialCardNotFound(card_id)
        return self._detail

    async def download_artifact(self, card_id: str) -> bytes | None:
        if self.missing:
            raise OfficialCardNotFound(card_id)
        return self._artifact


def _summary(**overrides) -> OfficialCardSummary:
    return OfficialCardSummary(
        id=_CARD_ID,
        title="ミオ",
        summary="カフェで働く大学生。",
        tags=("現代",),
        author="Yuralume",
        localized=True,
        image_url="https://app.yuralume.com/api/user"
                  "/v1/public/official-cards/mio-cafe/images/0",
        image_count=2,
        **overrides,
    )


def _detail(
    *,
    localized: bool = True,
    stale: bool = False,
    profile: dict | None = None,
) -> OfficialCardDetail:
    return OfficialCardDetail(
        id=_CARD_ID,
        title="ミオ",
        description="公式カード",
        tags=("現代",),
        author="Yuralume",
        note="アニメ調の profile 推奨",
        localized=localized,
        stale=stale,
        locale="ja",
        available_locales=("zh-Hant", "en", "ja"),
        profile={
            "name": "ミオ",
            "summary": "カフェで働く大学生。",
            "personality": ["明るい"],
            "companions": [{"name": "ハル", "role": "同僚"}],
        } if profile is None else profile,
        images=(
            OfficialCardImage(
                index=0,
                url="https://app.yuralume.com/api/user"
                    "/v1/public/official-cards/mio-cafe/images/0",
                content_type="image/png",
                width=1024,
                height=1536,
                variants=("w320", "w768"),
            ),
        ),
    )


def _official_artifact() -> bytes:
    """A real ``.lumecard`` in the card's source language (zh-TW)."""
    manifest = CharacterCardManifest(
        card=CharacterCardMeta(
            title="美緒 — 官方角色",
            author="Yuralume",
            description="官方卡",
            tags=["現代"],
        ),
        character=CharacterCardProfile(
            name="美緒",
            summary="咖啡廳打工女大生",
            personality=["開朗"],
            world_frame="modern",
            proactive_daily_limit=5,
        ),
    )
    return pack_character_card(
        manifest_json=manifest.model_dump_json(indent=2),
        stage_images=[],
        arc_templates=[],
    )


def _official_artifact_with_arc_template() -> bytes:
    """The same card, carrying a bundled arc template.

    A second prose surface with a second translator behind it — the one a
    profile-only assertion would never notice.
    """
    manifest = CharacterCardManifest(
        card=CharacterCardMeta(
            title="美緒 — 官方角色", author="Yuralume", description="官方卡",
        ),
        character=CharacterCardProfile(
            name="美緒", summary="咖啡廳打工女大生", world_frame="modern",
        ),
    )
    template = ArcTemplate.create(
        id="cafe_idol",
        title="咖啡廳偶像試鏡",
        premise="一段在咖啡廳打工的日常。",
        theme="ambition",
        tone="lighthearted",
        language="zh-TW",
        duration_days=14,
        beats=[
            ArcTemplateBeat.create(
                sequence=0, day_offset=0, title="傳單",
                summary="她撿到一張試鏡傳單。", tension="setup",
            ),
        ],
    )
    return pack_character_card(
        manifest_json=manifest.model_dump_json(indent=2),
        stage_images=[],
        arc_templates=[("cafe_idol.yaml", dump_arc_template_to_yaml(template))],
    )


def _write_local_pack(directory: Path, pack_id: str = "demo_local") -> None:
    manifest = CharacterCardManifest(
        card=CharacterCardMeta(title="本地示範卡", author="Tester"),
        character=CharacterCardProfile(name="小夏", summary="鄰居"),
    )
    (directory / f"{pack_id}.lumecard").write_bytes(
        pack_character_card(
            manifest_json=manifest.model_dump_json(indent=2),
            stage_images=[],
            arc_templates=[],
        ),
    )


def _build_service(
    directory: Path,
    *,
    catalog: _FakeCatalog | None,
    translator: _RecordingTranslator | None = None,
    arc_translator: _RecordingArcTranslator | None = None,
) -> tuple[CharacterCardPackService, InMemoryCharacterRepository]:
    char_repo = InMemoryCharacterRepository()
    character_service = CharacterService(
        char_repo,
        relationship_seed_repository=(
            InMemoryCharacterOperatorRelationshipSeedRepository()
        ),
    )
    import_service = CharacterCardImportService(
        character_service=character_service,
        character_image_service=CharacterImageService(
            character_repository=char_repo,
            uploads_dir=Path("."),
            object_storage=InMemoryObjectStorage(public_base_url="/uploads"),
        ),
        translator=translator,
        arc_template_repository=InMemoryArcTemplateRepository(),
        arc_template_translator=arc_translator,
    )
    service = CharacterCardPackService(
        catalog=CharacterCardPackCatalog(directories=[directory]),
        import_service=import_service,
        official_cards=(
            None if catalog is None
            else OfficialCardPackSource(
                catalog=catalog, import_service=import_service,
            )
        ),
    )
    return service, char_repo


# --------------------------------------------------------------------------- #
# browse
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_official_cards_and_local_packs_share_one_shelf(
    tmp_path: Path,
) -> None:
    _write_local_pack(tmp_path)
    catalog = _FakeCatalog(cards=[_summary()])
    service, _repo = _build_service(tmp_path, catalog=catalog)

    catalogue = await service.list_available(primary_language="ja-JP")

    # Official first, then local; one opaque id space with the Cloud ones
    # prefixed so preview / install can route without a second lookup.
    assert [card.pack_id for card in catalogue.cards] == [_CLOUD_REF, "demo_local"]
    assert [card.source for card in catalogue.cards] == ["cloud", "local"]
    assert catalogue.cards[0].image_urls == [
        "https://app.yuralume.com/api/user"
        "/v1/public/official-cards/mio-cafe/images/0",
    ]
    assert catalogue.cards[0].localized is True
    assert catalogue.official_cards_unavailable is False
    # The player's content language decides which approved translation the
    # catalog answers with.
    assert catalog.requested_locales == ["ja"]


@pytest.mark.asyncio
async def test_a_language_the_catalog_does_not_publish_is_forwarded_verbatim(
    tmp_path: Path,
) -> None:
    """Cloud then answers English and reports ``localized=false``.

    Asking for ``en`` on the player's behalf would come back
    ``localized=true`` and tell the UI to hide the translate toggle from the
    one player who cannot read what is on screen.
    """
    catalog = _FakeCatalog(cards=[])
    service, _repo = _build_service(tmp_path, catalog=catalog)

    await service.list_available(primary_language="ko-KR")

    assert catalog.requested_locales == ["ko-KR"]


@pytest.mark.asyncio
async def test_a_cloud_outage_empties_the_official_shelf_and_says_so(
    tmp_path: Path,
) -> None:
    _write_local_pack(tmp_path)
    service, _repo = _build_service(tmp_path, catalog=_FakeCatalog(cards=None))

    catalogue = await service.list_available(primary_language="zh-TW")

    # A mirror, not a wall: local packs are untouched and the UI is told
    # what is missing instead of being handed an error.
    assert [card.pack_id for card in catalogue.cards] == ["demo_local"]
    assert catalogue.official_cards_unavailable is True


@pytest.mark.asyncio
async def test_the_retired_shipping_directory_is_simply_empty(
    tmp_path: Path,
) -> None:
    """Nothing ships in the repo any more, and nothing breaks.

    With the catalog switched off (no source wired, the self-host opt-out)
    an empty local directory is an empty shelf and no error at all.
    """
    service, _repo = _build_service(tmp_path, catalog=None)

    catalogue = await service.list_available(primary_language="zh-TW")

    assert catalogue.cards == []
    assert catalogue.official_cards_unavailable is False


@pytest.mark.asyncio
async def test_official_cards_alone_are_a_complete_shelf(tmp_path: Path) -> None:
    service, _repo = _build_service(
        tmp_path, catalog=_FakeCatalog(cards=[_summary()]),
    )

    catalogue = await service.list_available(primary_language="ja-JP")

    assert [card.pack_id for card in catalogue.cards] == [_CLOUD_REF]


# --------------------------------------------------------------------------- #
# preview
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_previewing_an_official_card_never_calls_the_model(
    tmp_path: Path,
) -> None:
    translator = _RecordingTranslator()
    service, _repo = _build_service(
        tmp_path,
        catalog=_FakeCatalog(detail=_detail()),
        translator=translator,
    )

    preview = await service.preview(
        _CLOUD_REF, translate=True, target_language="ja-JP",
        primary_language="ja-JP",
    )

    # The catalog carries an approved translation per language; asking a
    # model to redo it would charge the player for work already done.
    assert translator.calls == []
    assert preview.pack_id == _CLOUD_REF
    assert preview.name == "ミオ"
    assert preview.personality == ["明るい"]
    assert [c.name for c in preview.companions] == ["ハル"]
    assert preview.note == "アニメ調の profile 推奨"
    assert preview.source == "cloud"
    assert preview.localized is True
    assert preview.stage_image_count == 1


@pytest.mark.asyncio
async def test_a_withdrawn_official_card_reads_as_not_found(tmp_path: Path) -> None:
    catalog = _FakeCatalog(detail=_detail())
    catalog.missing = True
    service, _repo = _build_service(tmp_path, catalog=catalog)

    with pytest.raises(CharacterCardPackNotFoundError):
        await service.preview(_CLOUD_REF, primary_language="ja-JP")


@pytest.mark.asyncio
async def test_an_unreachable_catalog_is_unavailable_not_missing(
    tmp_path: Path,
) -> None:
    """The two are different to a player: "gone" vs "try again".

    The route turns this into a 503; never a 500, and never a 404 that would
    claim an official card had been withdrawn because Cloud had a bad
    minute.
    """
    service, _repo = _build_service(tmp_path, catalog=_FakeCatalog(detail=None))

    with pytest.raises(OfficialCardUnavailableError):
        await service.preview(_CLOUD_REF, primary_language="ja-JP")


# --------------------------------------------------------------------------- #
# install
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_installing_an_official_card_lands_the_approved_translation(
    tmp_path: Path,
) -> None:
    translator = _RecordingTranslator()
    service, char_repo = _build_service(
        tmp_path,
        catalog=_FakeCatalog(detail=_detail(), artifact=_official_artifact()),
        translator=translator,
    )

    result = await service.install(
        _CLOUD_REF, user_id=_INSTALLER, primary_language="ja-JP",
    )

    # Prose from the approved translation, structure from the .lumecard, and
    # not one model call anywhere in between.
    assert translator.calls == []
    assert result.character.name == "ミオ"
    assert result.character.summary == "カフェで働く大学生。"
    assert result.character.personality == ["明るい"]
    assert result.character.proactive_daily_limit == 5
    entity = await char_repo.get(result.character.id)
    assert entity is not None and entity.user_id == _INSTALLER


@pytest.mark.asyncio
async def test_an_approved_translation_wins_even_when_translate_was_asked_for(
    tmp_path: Path,
) -> None:
    translator = _RecordingTranslator()
    service, _repo = _build_service(
        tmp_path,
        catalog=_FakeCatalog(detail=_detail(), artifact=_official_artifact()),
        translator=translator,
    )

    result = await service.install(
        _CLOUD_REF,
        user_id=_INSTALLER,
        translate=True,
        target_language="ja-JP",
        primary_language="ja-JP",
    )

    assert translator.calls == []
    assert result.character.name == "ミオ"


@pytest.mark.asyncio
async def test_a_fallback_language_card_installs_what_was_browsed_not_an_llm_pass(
    tmp_path: Path,
) -> None:
    """An official card never reaches a model, whatever the toggle said.

    The catalog fell back to another language, so what the player read in
    the gallery was that fallback text. Installing it is the honest answer:
    the alternative charged them for a translation they were never offered
    (the toggle is hidden on cloud cards) and landed prose that does not
    match the card they clicked.
    """
    translator = _RecordingTranslator()
    service, _repo = _build_service(
        tmp_path,
        catalog=_FakeCatalog(
            detail=_detail(localized=False), artifact=_official_artifact(),
        ),
        translator=translator,
    )

    result = await service.install(
        _CLOUD_REF,
        user_id=_INSTALLER,
        translate=True,
        target_language="ko-KR",
        primary_language="ko-KR",
    )

    assert translator.calls == []
    assert result.character.name == "ミオ"


@pytest.mark.asyncio
async def test_a_bundled_arc_template_is_not_an_llm_side_door(
    tmp_path: Path,
) -> None:
    """The card's second prose surface, and its second charge.

    ``translate`` used to travel all the way into the bundled-template
    loop, so a cloud install with the flag set paid for arc-template
    translation even though the profile path had already decided not to
    call a model.
    """
    translator = _RecordingTranslator()
    arc_translator = _RecordingArcTranslator()
    service, _repo = _build_service(
        tmp_path,
        catalog=_FakeCatalog(
            detail=_detail(),
            artifact=_official_artifact_with_arc_template(),
        ),
        translator=translator,
        arc_translator=arc_translator,
    )

    result = await service.install(
        _CLOUD_REF,
        user_id=_INSTALLER,
        translate=True,
        target_language="ja-JP",
        primary_language="ja-JP",
    )

    assert translator.calls == []
    assert arc_translator.calls == []
    assert result.landed_arc_template_ids == ["cafe_idol"]


@pytest.mark.asyncio
async def test_a_stale_translation_is_refused_rather_than_pasted_onto_a_new_card(
    tmp_path: Path,
) -> None:
    """The corruption a length check cannot catch.

    A ``stale`` payload was approved against an *older* artifact. Merging it
    into the re-uploaded card matches by list position, so equal-length
    lists sail straight through the merge policy and land a character whose
    fields are half one version and half another. The card's own text is
    the only coherent thing left to install.
    """
    translator = _RecordingTranslator()
    service, _repo = _build_service(
        tmp_path,
        catalog=_FakeCatalog(
            detail=_detail(stale=True), artifact=_official_artifact(),
        ),
        translator=translator,
    )

    result = await service.install(
        _CLOUD_REF, user_id=_INSTALLER, primary_language="ja-JP",
    )

    # The artifact's own (zh-TW) prose, whole — not yesterday's Japanese
    # sentences over today's fields.
    assert result.character.name == "美緒"
    assert result.character.summary == "咖啡廳打工女大生"
    assert result.character.personality == ["開朗"]
    # And still no model: "we cannot trust this translation" is not a
    # licence to buy a new one.
    assert translator.calls == []


@pytest.mark.asyncio
async def test_a_failed_artifact_download_is_unavailable_not_a_half_import(
    tmp_path: Path,
) -> None:
    service, char_repo = _build_service(
        tmp_path, catalog=_FakeCatalog(detail=_detail(), artifact=None),
    )

    with pytest.raises(OfficialCardUnavailableError):
        await service.install(
            _CLOUD_REF, user_id=_INSTALLER, primary_language="ja-JP",
        )
    assert await char_repo.list_for_user(_INSTALLER) == []


@pytest.mark.asyncio
async def test_installing_reads_the_detail_past_the_cache(tmp_path: Path) -> None:
    """The two halves of an install must describe the same version.

    The artifact is never cached — every install downloads it live — so a
    cached detail is a window in which Cloud's freshly re-uploaded bytes
    arrive next to a document that still says ``stale=false``. The
    translation then gets merged into a character it was never approved
    against, on exactly the flag that exists to stop that.
    """
    catalog = _FakeCatalog(detail=_detail(), artifact=_official_artifact())
    service, _repo = _build_service(tmp_path, catalog=catalog)

    await service.install(
        _CLOUD_REF, user_id=_INSTALLER, primary_language="ja-JP",
    )

    assert catalog.detail_freshness == [True]


@pytest.mark.asyncio
async def test_browsing_still_goes_through_the_cache(tmp_path: Path) -> None:
    """Only the install path pays for freshness.

    Thousands of self-hosted deployments read this catalog; a gallery that
    bypassed the cache would turn every card face into a Cloud round trip,
    which is the load the TTL exists to flatten. And a reader loses nothing
    to a ten-minute-old description.
    """
    catalog = _FakeCatalog(cards=[_summary()], detail=_detail())
    service, _repo = _build_service(tmp_path, catalog=catalog)

    await service.list_available(primary_language="ja-JP")
    await service.preview(_CLOUD_REF, primary_language="ja-JP")

    assert catalog.detail_freshness == [False]


@pytest.mark.asyncio
async def test_local_packs_are_untouched_by_all_of_this(tmp_path: Path) -> None:
    _write_local_pack(tmp_path)
    translator = _RecordingTranslator()
    service, _repo = _build_service(
        tmp_path, catalog=_FakeCatalog(cards=[]), translator=translator,
    )

    result = await service.install(
        "demo_local", user_id=_INSTALLER, primary_language="ja-JP",
    )

    assert result.character.name == "小夏"
    assert translator.calls == []
