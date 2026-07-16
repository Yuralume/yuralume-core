from datetime import datetime, timezone

import pytest

from kokoro_link.application.dto.character import (
    CreateCharacterRequest,
    InitialRelationshipPayload,
    InitialRelationshipSafeUserProfilePayload,
)
from kokoro_link.application.services.character_service import (
    CharacterService,
    CharacterValidationError,
)
from kokoro_link.domain.value_objects.profile_field import ProfileField
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_initial_relationship import (
    InMemoryCharacterOperatorRelationshipSeedRepository,
)


class _CapturingPersonaRepo:
    def __init__(self) -> None:
        self.fields: list[ProfileField] = []
        self.deleted: list[str] = []

    async def upsert_field(
        self, character_id: str, operator_id: str, field: ProfileField,
    ) -> ProfileField:
        assert field.character_id == character_id
        assert operator_id
        self.fields.append(field)
        return field

    async def delete_for_character(self, character_id: str) -> int:
        self.deleted.append(character_id)
        return 0


class _FailingSeedRepo(InMemoryCharacterOperatorRelationshipSeedRepository):
    async def save(self, seed):  # noqa: ANN001
        raise RuntimeError("seed store failed")


@pytest.mark.asyncio
async def test_create_character_persists_initial_relationship_seed() -> None:
    character_repo = InMemoryCharacterRepository()
    seed_repo = InMemoryCharacterOperatorRelationshipSeedRepository()
    service = CharacterService(
        character_repo,
        relationship_seed_repository=seed_repo,
    )

    created = await service.create_character(
        CreateCharacterRequest(
            name="澄香",
            summary="只描述角色本人。",
            initial_relationship=InitialRelationshipPayload(
                relationship_label="剛認識的朋友",
                known_context="在創角時設定為可慢慢熟悉的朋友。",
                living_arrangement="分開住，但常在附近活動。",
                user_address_name="小夏",
                character_address_name="澄香",
                tone_distance="友善但有分寸",
                familiarity_boundary="不要假裝已經有共同回憶。",
                schedule_involvement_policy="invite_required",
                proactive_permission=True,
                proactive_cadence_hint="一週一兩次，先輕聲問候。",
                user_profile_notes="喜歡下班後聊咖啡。",
            ),
        )
    )

    loaded = await seed_repo.get(created.id, "default")
    assert loaded is not None
    assert loaded.relationship_label == "剛認識的朋友"
    assert loaded.living_arrangement == "分開住，但常在附近活動。"
    assert loaded.schedule_involvement_policy == "invite_required"
    assert loaded.proactive_permission is True
    assert "共同回憶" in loaded.familiarity_boundary
    assert created.summary == "只描述角色本人。"
    assert await character_repo.get(created.id) is not None


@pytest.mark.asyncio
async def test_create_character_syncs_only_safe_profile_fields() -> None:
    persona_repo = _CapturingPersonaRepo()
    service = CharacterService(
        InMemoryCharacterRepository(),
        relationship_seed_repository=InMemoryCharacterOperatorRelationshipSeedRepository(),
        operator_persona_repository=persona_repo,  # type: ignore[arg-type]
    )

    await service.create_character(
        CreateCharacterRequest(
            name="澄香",
            initial_relationship=InitialRelationshipPayload(
                relationship_label="朋友",
                safe_user_profile=InitialRelationshipSafeUserProfilePayload(
                    name="夏彌",
                    nickname="小夏",
                    occupation="設計師",
                    interests=["咖啡", "散步", "咖啡"],
                    routine="下班後通常比較能聊天",
                    life_goals=["整理作品集"],
                ),
            ),
        )
    )

    keys = {field.field_key for field in persona_repo.fields}
    assert keys == {
        "name",
        "nickname",
        "occupation",
        "interests",
        "routine",
        "life_goals",
    }
    assert "relationship_status" not in keys
    assert all(field.layer in {1, 2} for field in persona_repo.fields)
    interests = next(field for field in persona_repo.fields if field.field_key == "interests")
    assert interests.value == "咖啡、散步"
    assert interests.source == "user_explicit"


@pytest.mark.asyncio
async def test_create_character_rolls_back_character_when_seed_save_fails() -> None:
    character_repo = InMemoryCharacterRepository()
    service = CharacterService(
        character_repo,
        relationship_seed_repository=_FailingSeedRepo(),
    )

    with pytest.raises(RuntimeError):
        await service.create_character(
            CreateCharacterRequest(
                name="澄香",
                initial_relationship=InitialRelationshipPayload(
                    relationship_label="朋友",
                ),
            )
        )

    assert await character_repo.list() == []


@pytest.mark.asyncio
async def test_safe_profile_requires_persona_repository_when_values_present() -> None:
    service = CharacterService(
        InMemoryCharacterRepository(),
        relationship_seed_repository=InMemoryCharacterOperatorRelationshipSeedRepository(),
    )

    with pytest.raises(CharacterValidationError):
        await service.create_character(
            CreateCharacterRequest(
                name="澄香",
                initial_relationship=InitialRelationshipPayload(
                    safe_user_profile=InitialRelationshipSafeUserProfilePayload(
                        name="夏彌",
                    ),
                ),
            )
        )


def test_initial_relationship_payload_builds_seed_without_layer4_fields() -> None:
    payload = InitialRelationshipPayload(
        relationship_label="朋友",
        living_arrangement="住在使用者家裡",
        schedule_involvement_policy="mention_only",
    )
    seed = payload.to_seed(
        character_id="char-1",
        operator_id="op-1",
        now=datetime(2026, 6, 11, tzinfo=timezone.utc),
    )
    assert seed.character_id == "char-1"
    assert seed.operator_id == "op-1"
    assert seed.relationship_label == "朋友"
    assert seed.living_arrangement == "住在使用者家裡"
    assert not hasattr(seed, "interaction_strength")


def test_initial_relationship_seed_trims_living_arrangement_and_counts_as_non_empty() -> None:
    seed = InitialRelationshipPayload(
        living_arrangement="  住在使用者家裡  ",
    ).to_seed(
        character_id="char-1",
        operator_id="op-1",
        now=datetime(2026, 6, 11, tzinfo=timezone.utc),
    )

    assert seed.living_arrangement == "住在使用者家裡"
    assert seed.is_empty is False
