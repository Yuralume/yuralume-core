from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import sessionmaker

from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.character_operator_relationship_seed import (
    CharacterOperatorRelationshipSeed,
)
from kokoro_link.domain.entities.operator_profile import (
    DEFAULT_OPERATOR_ID,
    OperatorProfile,
)
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.persistence.sa_character_repository import (
    SACharacterRepository,
)
from kokoro_link.infrastructure.persistence.sa_initial_relationship_repository import (
    SACharacterOperatorRelationshipSeedRepository,
)
from kokoro_link.infrastructure.persistence.sa_operator_profile_repository import (
    SAOperatorProfileRepository,
)


async def _setup(session_factory: sessionmaker) -> str:
    profile_repo = SAOperatorProfileRepository(session_factory)
    if await profile_repo.get_default() is None:
        await profile_repo.save(
            OperatorProfile(id=DEFAULT_OPERATOR_ID, display_name="丹尼"),
        )
    character_repo = SACharacterRepository(session_factory)
    character = Character.create(
        name="澄香",
        summary="",
        personality=[],
        interests=[],
        speaking_style="",
        boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
    )
    await character_repo.save(character)
    return character.id


@pytest.mark.asyncio
async def test_seed_round_trip_and_delete(session_factory: sessionmaker) -> None:
    character_id = await _setup(session_factory)
    repo = SACharacterOperatorRelationshipSeedRepository(session_factory)
    now = datetime(2026, 6, 11, tzinfo=timezone.utc)
    seed = CharacterOperatorRelationshipSeed(
        character_id=character_id,
        operator_id=DEFAULT_OPERATOR_ID,
        relationship_label="剛認識的朋友",
        known_context="創角時確認可知道的背景。",
        living_arrangement="住在使用者家裡。",
        user_address_name="小夏",
        character_address_name="澄香",
        tone_distance="友善但有分寸",
        familiarity_boundary="不可杜撰共同回憶。",
        schedule_involvement_policy="mention_only",
        proactive_permission=True,
        proactive_cadence_hint="低頻問候",
        user_profile_notes="喜歡咖啡。",
        confirmed_by_user=True,
        created_at=now,
        updated_at=now,
    )

    await repo.save(seed)
    loaded = await repo.get(character_id, DEFAULT_OPERATOR_ID)

    assert loaded is not None
    assert loaded.relationship_label == "剛認識的朋友"
    assert loaded.living_arrangement == "住在使用者家裡。"
    assert loaded.schedule_involvement_policy == "mention_only"
    assert loaded.proactive_permission is True

    removed = await repo.delete_for_character(character_id)
    assert removed == 1
    assert await repo.get(character_id, DEFAULT_OPERATOR_ID) is None
