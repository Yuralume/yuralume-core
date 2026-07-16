"""Unit tests for the SillyTavern → CharacterCardManifest converter.

Uses a stub :class:`SillyTavernNormalizerPort` so the conversion contract
is verified without an LLM: deterministic metadata mapping, the
relationship red line (``known_context`` never in the manifest), dropped
fields, and PNG → stage portrait.
"""

from __future__ import annotations

import pytest

from kokoro_link.application.services.sillytavern_convert_service import (
    DROPPED_CHARACTER_BOOK,
    DROPPED_EXTRA_ASSETS,
    DROPPED_GREETINGS,
    SillyTavernConvertService,
)
from kokoro_link.contracts.sillytavern_normalizer import (
    SillyTavernNormalizedProfile,
    SillyTavernNormalizerInput,
)
from kokoro_link.infrastructure.character_card.sillytavern import (
    SillyTavernCard,
    SillyTavernCharacterBook,
)


class _StubNormalizer:
    def __init__(self, profile: SillyTavernNormalizedProfile) -> None:
        self._profile = profile
        self.calls: list[SillyTavernNormalizerInput] = []

    async def normalize(
        self,
        payload: SillyTavernNormalizerInput,
        *,
        operator_id: str | None = None,
    ) -> SillyTavernNormalizedProfile:
        self.calls.append(payload)
        return self._profile


def _card(**overrides: object) -> SillyTavernCard:
    base = dict(
        spec="chara_card_v2",
        name="Mio",
        description="A cheerful barista.",
        personality="warm",
        scenario="You meet her at the cafe.",
        first_mes="Welcome!",
        mes_example="{{char}}: hi~",
        creator_notes="anime profile suggested",
        creator="cafe_author",
        tags=["modern", "slice-of-life"],
        alternate_greetings=["Oh, you again!"],
    )
    base.update(overrides)
    return SillyTavernCard(**base)


def _normalized() -> SillyTavernNormalizedProfile:
    return SillyTavernNormalizedProfile(
        summary="A warm barista who loves latte art.",
        personality=["warm", "energetic"],
        interests=["coffee", "singing"],
        boundaries=["no rudeness"],
        aspirations=["open her own cafe"],
        appearance="brown bob, hazel eyes, cafe apron",
        speaking_style="bubbly, lots of tildes",
        suggested_known_context="You've just walked into her cafe.",
    )


@pytest.mark.asyncio
async def test_metadata_maps_deterministically() -> None:
    service = SillyTavernConvertService(normalizer=_StubNormalizer(_normalized()))
    result = await service.to_manifest(_card(), png=None)

    manifest = result.manifest
    assert manifest.character.name == "Mio"
    assert manifest.card.title == "Mio"
    assert manifest.card.author == "cafe_author"
    assert manifest.card.note == "anime profile suggested"
    assert manifest.card.tags == ["modern", "slice-of-life"]


@pytest.mark.asyncio
async def test_normalized_fields_land_in_profile() -> None:
    service = SillyTavernConvertService(normalizer=_StubNormalizer(_normalized()))
    result = await service.to_manifest(_card(), png=None)

    profile = result.manifest.character
    assert profile.summary == "A warm barista who loves latte art."
    assert profile.personality == ["warm", "energetic"]
    assert profile.interests == ["coffee", "singing"]
    assert profile.boundaries == ["no rudeness"]
    assert profile.aspirations == ["open her own cafe"]
    assert profile.appearance == "brown bob, hazel eyes, cafe apron"
    assert profile.speaking_style == "bubbly, lots of tildes"


@pytest.mark.asyncio
async def test_known_context_never_enters_manifest() -> None:
    service = SillyTavernConvertService(normalizer=_StubNormalizer(_normalized()))
    result = await service.to_manifest(_card(), png=None)

    # The suggested context rides alongside — never inside the manifest.
    assert result.suggested_known_context == "You've just walked into her cafe."
    dumped = result.manifest.model_dump_json()
    assert "walked into her cafe" not in dumped


@pytest.mark.asyncio
async def test_dropped_fields_reported() -> None:
    card = _card(
        character_book=SillyTavernCharacterBook(name="Lore", entry_count=3),
        has_assets=True,
    )
    service = SillyTavernConvertService(normalizer=_StubNormalizer(_normalized()))
    result = await service.to_manifest(card, png=None)

    assert DROPPED_CHARACTER_BOOK in result.dropped_fields
    assert DROPPED_GREETINGS in result.dropped_fields
    assert DROPPED_EXTRA_ASSETS in result.dropped_fields


@pytest.mark.asyncio
async def test_empty_lorebook_not_reported_dropped() -> None:
    card = _card(
        character_book=SillyTavernCharacterBook(name="", entry_count=0),
        first_mes="",
        alternate_greetings=[],
        has_assets=False,
    )
    service = SillyTavernConvertService(normalizer=_StubNormalizer(_normalized()))
    result = await service.to_manifest(card, png=None)

    assert result.dropped_fields == []


@pytest.mark.asyncio
async def test_png_becomes_single_stage_portrait() -> None:
    service = SillyTavernConvertService(normalizer=_StubNormalizer(_normalized()))
    result = await service.to_manifest(_card(), png=b"\x89PNG\r\n\x1a\n binary")

    assert result.manifest.stage_images == ["assets/stage/0.png"]
    assert result.png == b"\x89PNG\r\n\x1a\n binary"


@pytest.mark.asyncio
async def test_no_png_means_no_stage_image() -> None:
    service = SillyTavernConvertService(normalizer=_StubNormalizer(_normalized()))
    result = await service.to_manifest(_card(), png=None)

    assert result.manifest.stage_images == []


@pytest.mark.asyncio
async def test_normalizer_receives_tone_evidence() -> None:
    stub = _StubNormalizer(_normalized())
    service = SillyTavernConvertService(normalizer=stub)
    await service.to_manifest(_card(), png=None, operator_primary_language="ja-JP")

    assert len(stub.calls) == 1
    call = stub.calls[0]
    assert call.first_mes == "Welcome!"
    assert call.mes_example == "{{char}}: hi~"
    assert call.operator_primary_language == "ja-JP"


@pytest.mark.asyncio
async def test_missing_name_uses_locale_fallback() -> None:
    service = SillyTavernConvertService(normalizer=_StubNormalizer(_normalized()))
    result = await service.to_manifest(
        _card(name=""), png=None, operator_primary_language="en-US",
    )
    assert result.manifest.character.name == "Imported Character"
