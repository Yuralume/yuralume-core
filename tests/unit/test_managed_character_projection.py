"""託管角色（IP 合作卡）的讀遮蔽與寫保護（EC2-A）.

Three invariants, in the order they matter:

1. An ordinary character — the only kind a self-host install ever has —
   is untouched by everything in this batch, response and update path
   alike.
2. A managed character's persona never leaves the server through the
   character projection, and the mask is applied in the one place all
   four endpoints share.
3. A stale client that PATCHes the whole form back cannot blank that
   persona: the real values are refused loudly, the masked blanks are
   dropped, and the player's own fields still save.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest

from kokoro_link.application.dto.character import (
    CharacterCompanionPayload,
    CharacterDispositionPayload,
    CharacterPersonalityTypePayload,
    CharacterResponse,
    UpdateCharacterRequest,
    VoiceProfilePayload,
)
from kokoro_link.application.dto.character_managed_view import (
    MANAGED_MASKED_RESPONSE_FIELDS,
)
from kokoro_link.application.services.character_service import (
    CharacterService,
    CharacterValidationError,
)
from kokoro_link.application.services.managed_character_update_policy import (
    MANAGED_WRITABLE_UPDATE_FIELDS,
    managed_protected_update_fields,
    review_managed_update,
)
from kokoro_link.domain.entities.character import Character, CharacterLora
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.companion import CharacterCompanion
from kokoro_link.domain.value_objects.personality_type import (
    CharacterPersonalityType,
)
from kokoro_link.domain.value_objects.voice_profile import VoiceProfile
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)


CARD_ID = "cloud-card-mio"


def _character(*, origin: str | None = None) -> Character:
    """A fully-populated character, so masking has something to hide."""
    return Character.create(
        name="美緒",
        summary="咖啡廳打工的女大生",
        personality=["溫柔", "愛操心"],
        interests=["拉花", "獨立樂團"],
        speaking_style="輕快、句尾常帶語助詞",
        boundaries=["不談前男友"],
        aspirations=["開自己的店"],
        appearance="black bob cut, apron, hazel eyes",
        gender_identity="女性",
        third_person_pronoun="她",
        visual_gender_presentation="feminine young woman",
        visual_subject_type="human",
        visual_generation_style="anime",
        date_of_birth=date(2003, 4, 18),
        image_urls=["characters/mio/stage-1.png"],
        loras=[CharacterLora(name="mio_v3", strength=0.8)],
        world_topics=["咖啡", "獨立音樂"],
        excluded_topics=["政治"],
        state=CharacterState(
            emotion="calm", affection=42, fatigue=10, trust=50, energy=90,
        ),
        companions=[
            CharacterCompanion.create(id_=None, name="店長", role="上司"),
        ],
        operator_pace_preference="balanced",
        feed_daily_limit=2,
        personality_type=CharacterPersonalityType(
            system="mbti_16", code="INFJ", source="user_explicit", confidence=1.0,
        ),
        origin_official_card_id=origin,
    )


def _managed_response() -> CharacterResponse:
    return CharacterResponse.from_domain(_character(origin=CARD_ID))


# ======================================================================
# 1. Ordinary characters — the self-host red line
# ======================================================================


def test_ordinary_character_is_not_managed_and_keeps_every_field() -> None:
    payload = CharacterResponse.from_domain(_character()).model_dump()

    assert payload["personality"] == ["溫柔", "愛操心"]
    assert payload["appearance"] == "black bob cut, apron, hazel eyes"
    assert payload["visual_subject_type"] == "human"
    assert payload["date_of_birth"] == date(2003, 4, 18)
    assert payload["world_topics"] == ["咖啡", "獨立音樂"]
    assert payload["personality_type"]["code"] == "INFJ"


def test_ordinary_character_response_does_not_grow_a_managed_key() -> None:
    """Byte-parity: no new key appears for the characters self-host has."""
    payload = CharacterResponse.from_domain(_character()).model_dump()
    serialised = CharacterResponse.from_domain(_character()).model_dump_json()

    assert "managed" not in payload
    assert "managed" not in serialised


def test_is_managed_is_the_single_derivation() -> None:
    assert _character().is_managed is False
    assert _character(origin="   ").is_managed is False  # blank → no binding
    assert _character(origin=CARD_ID).is_managed is True


# ======================================================================
# 2. The mask
# ======================================================================


def test_managed_response_masks_every_protected_field() -> None:
    payload = _managed_response().model_dump()

    assert payload["managed"] is True
    assert payload["personality"] == []
    assert payload["interests"] == []
    assert payload["speaking_style"] == ""
    assert payload["boundaries"] == []
    assert payload["aspirations"] == []
    assert payload["appearance"] == ""
    assert payload["gender_identity"] == ""
    assert payload["third_person_pronoun"] == ""
    assert payload["visual_gender_presentation"] == ""
    assert payload["visual_subject_type"] == "auto"
    assert payload["visual_generation_style"] == ""
    assert payload["date_of_birth"] is None
    assert payload["loras"] == []
    assert payload["world_topics"] == []
    assert payload["excluded_topics"] == []
    assert payload["personality_type"]["code"] == ""


def test_managed_response_keeps_the_display_set() -> None:
    payload = _managed_response().model_dump()

    assert payload["name"] == "美緒"
    assert payload["summary"] == "咖啡廳打工的女大生"
    assert payload["image_urls"] == ["characters/mio/stage-1.png"]
    assert payload["state"]["affection"] == 42
    assert [c["name"] for c in payload["companions"]] == ["店長"]
    assert payload["disposition"]["candor"] == "medium"
    assert payload["proactive_rhythm"] == "balanced"
    assert payload["feed_daily_limit"] == 2
    assert payload["operator_pace_preference"] == "balanced"


def test_mask_covers_exactly_the_declared_field_set() -> None:
    """The declared set is the contract; nothing masks by accident."""
    character = _character()
    ordinary = CharacterResponse.from_domain(character).model_dump()
    managed = CharacterResponse.from_domain(
        replace(character, origin_official_card_id=CARD_ID),
    ).model_dump()

    differing = {
        name
        for name in ordinary
        if ordinary[name] != managed.get(name)
    }
    assert differing == set(MANAGED_MASKED_RESPONSE_FIELDS)


# ======================================================================
# 3. The write boundary
# ======================================================================


def test_writable_allowlist_names_real_dto_fields() -> None:
    assert MANAGED_WRITABLE_UPDATE_FIELDS <= set(
        UpdateCharacterRequest.model_fields,
    )


def test_every_masked_field_is_also_write_protected() -> None:
    """A field can never be invisible-but-writable."""
    protected = managed_protected_update_fields()
    updatable_masked = MANAGED_MASKED_RESPONSE_FIELDS & set(
        UpdateCharacterRequest.model_fields,
    )
    assert updatable_masked <= protected


def test_identity_art_and_voice_are_protected_too() -> None:
    protected = managed_protected_update_fields()
    for field in (
        "name", "summary", "image_urls", "voice_profile",
        "arc_template_id", "arc_series_id",
    ):
        assert field in protected, field


def test_review_refuses_real_values_on_protected_fields() -> None:
    review = review_managed_update(UpdateCharacterRequest(
        name="我改的名字",
        personality=["cold"],
        voice_profile=VoiceProfilePayload(voice_id="other-voice"),
        companions=[CharacterCompanionPayload(name="室友")],
    ))

    assert review.rejected_fields == ("name", "personality", "voice_profile")


def test_review_drops_masked_blanks_and_keeps_player_fields() -> None:
    """The stale-client shape: everything the mask emptied, echoed back."""
    review = review_managed_update(UpdateCharacterRequest(
        personality=[],
        interests=[],
        speaking_style="",
        appearance="",
        date_of_birth=None,
        visual_generation_style="",
        personality_type=CharacterPersonalityTypePayload(),
        disposition=CharacterDispositionPayload(candor="high"),
        proactive_daily_limit=1,
    ))

    assert review.rejected_fields == ()
    assert review.payload.model_fields_set == {
        "disposition", "proactive_daily_limit",
    }
    assert review.payload.personality is None
    assert review.payload.proactive_daily_limit == 1


@pytest.mark.asyncio
async def test_stale_client_full_form_patch_cannot_blank_the_persona() -> None:
    repository = InMemoryCharacterRepository()
    service = CharacterService(repository)
    managed = _character(origin=CARD_ID)
    await repository.save(managed)

    with pytest.raises(CharacterValidationError) as excinfo:
        await service.update_character(
            managed.id,
            UpdateCharacterRequest(
                name="美緒",
                summary="咖啡廳打工的女大生",
                personality=[],
                interests=[],
                speaking_style="",
                boundaries=[],
                aspirations=[],
                appearance="",
                visual_subject_type="auto",
                date_of_birth=None,
                personality_type=CharacterPersonalityTypePayload(),
                state=None,
                companions=[],
            ),
        )

    assert "name" in str(excinfo.value)
    stored = await repository.get(managed.id)
    assert stored is not None
    assert stored.personality == ["溫柔", "愛操心"]
    assert stored.appearance == "black bob cut, apron, hazel eyes"
    assert stored.date_of_birth == date(2003, 4, 18)


@pytest.mark.asyncio
async def test_managed_patch_applies_the_player_owned_fields() -> None:
    repository = InMemoryCharacterRepository()
    service = CharacterService(repository)
    managed = _character(origin=CARD_ID)
    await repository.save(managed)

    updated = await service.update_character(
        managed.id,
        UpdateCharacterRequest(
            # Masked blanks the client echoed back — dropped, not applied.
            personality=[],
            appearance="",
            date_of_birth=None,
            # The player's own knobs — applied.
            proactive_daily_limit=1,
            proactive_cooldown_minutes=180,
            companions=[CharacterCompanionPayload(name="室友")],
        ),
    )

    assert updated is not None
    assert updated.managed is True
    assert updated.proactive_rhythm == "quiet"
    assert [c.name for c in updated.companions] == ["室友"]

    stored = await repository.get(managed.id)
    assert stored is not None
    assert stored.personality == ["溫柔", "愛操心"]
    assert stored.appearance == "black bob cut, apron, hazel eyes"


@pytest.mark.asyncio
async def test_ordinary_character_patch_is_unchanged() -> None:
    repository = InMemoryCharacterRepository()
    service = CharacterService(repository)
    ordinary = _character()
    await repository.save(ordinary)

    updated = await service.update_character(
        ordinary.id,
        UpdateCharacterRequest(personality=["cold"], appearance="new look"),
    )

    assert updated is not None
    assert updated.personality == ["cold"]
    assert updated.appearance == "new look"

    stored = await repository.get(ordinary.id)
    assert stored is not None
    assert stored.origin_official_card_id is None


def test_voice_profile_is_read_only_not_hidden() -> None:
    """Managed voice shows (so the player knows it) but never changes."""
    character = Character.create(
        name="美緒",
        summary="",
        personality=[],
        interests=[],
        speaking_style="",
        boundaries=[],
        state=CharacterState(
            emotion="calm", affection=0, fatigue=0, trust=0, energy=100,
        ),
        origin_official_card_id=CARD_ID,
    )
    voiced = character.with_voice_profile(
        VoiceProfile.from_payload({"enabled": True, "voice_id": "ip-voice"}),
    )

    response = CharacterResponse.from_domain(voiced)
    assert response.voice_profile is not None
    assert response.voice_profile.voice_id == "ip-voice"
    assert "voice_profile" in managed_protected_update_fields()
