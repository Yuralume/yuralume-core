from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import sessionmaker

from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.operator_profile import (
    DEFAULT_OPERATOR_ID,
    OperatorProfile,
)
from kokoro_link.domain.entities.persona_curiosity import (
    PERSONA_CURIOSITY_STATUS_ANSWERED,
    PERSONA_CURIOSITY_STATUS_ASKED,
    PERSONA_CURIOSITY_SURFACE_CHAT,
    PersonaCuriosityAttempt,
)
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.persistence.sa_character_repository import (
    SACharacterRepository,
)
from kokoro_link.infrastructure.persistence.sa_operator_profile_repository import (
    SAOperatorProfileRepository,
)
from kokoro_link.infrastructure.persistence.sa_persona_curiosity_repository import (
    SAPersonaCuriosityRepository,
)


_NOW = datetime(2026, 6, 9, 12, 0, tzinfo=timezone.utc)


async def _ensure_operator(session_factory: sessionmaker) -> None:
    repo = SAOperatorProfileRepository(session_factory)
    if await repo.get_default() is None:
        await repo.save(OperatorProfile(id=DEFAULT_OPERATOR_ID, display_name="User"))


async def _ensure_character(session_factory: sessionmaker, character_id: str) -> None:
    repo = SACharacterRepository(session_factory)
    if await repo.get(character_id) is None:
        await repo.save(
            Character(
                id=character_id,
                name=f"Char {character_id}",
                summary="",
                personality=[],
                interests=[],
                speaking_style="",
                boundaries=[],
                state=CharacterState(
                    emotion="neutral",
                    affection=50,
                    fatigue=0,
                    trust=50,
                    energy=100,
                ),
            ),
        )


@pytest.mark.asyncio
async def test_sa_persona_curiosity_round_trip_and_pair_isolation(
    session_factory: sessionmaker,
) -> None:
    await _ensure_operator(session_factory)
    await _ensure_character(session_factory, "char-A")
    await _ensure_character(session_factory, "char-B")
    repo = SAPersonaCuriosityRepository(session_factory)

    await repo.add(
        PersonaCuriosityAttempt.new(
            character_id="char-A",
            operator_id=DEFAULT_OPERATOR_ID,
            surface=PERSONA_CURIOSITY_SURFACE_CHAT,
            target_layer=2,
            target_topic="routine",
            question_intent="learn daily rhythm",
            status=PERSONA_CURIOSITY_STATUS_ASKED,
            created_at=_NOW - timedelta(minutes=5),
            metadata={"source": "unit"},
        ),
    )
    await repo.add(
        PersonaCuriosityAttempt.new(
            character_id="char-B",
            operator_id=DEFAULT_OPERATOR_ID,
            surface=PERSONA_CURIOSITY_SURFACE_CHAT,
            target_layer=1,
            target_topic="nickname",
            question_intent="learn nickname",
            created_at=_NOW,
        ),
    )

    listed = await repo.list_recent("char-A", DEFAULT_OPERATOR_ID, limit=10)

    assert len(listed) == 1
    assert listed[0].target_topic == "routine"
    assert listed[0].metadata == {"source": "unit"}


@pytest.mark.asyncio
async def test_sa_persona_curiosity_marks_status(
    session_factory: sessionmaker,
) -> None:
    await _ensure_operator(session_factory)
    await _ensure_character(session_factory, "char-A")
    repo = SAPersonaCuriosityRepository(session_factory)
    attempt = await repo.add(
        PersonaCuriosityAttempt.new(
            character_id="char-A",
            operator_id=DEFAULT_OPERATOR_ID,
            surface=PERSONA_CURIOSITY_SURFACE_CHAT,
            target_layer=2,
            target_topic="routine",
            question_intent="learn daily rhythm",
            status=PERSONA_CURIOSITY_STATUS_ASKED,
            created_at=_NOW,
        ),
    )

    changed = await repo.mark_status(
        attempt.id,
        PERSONA_CURIOSITY_STATUS_ANSWERED,
        response_turn_id="turn-2",
    )

    assert changed
    listed = await repo.list_recent("char-A", DEFAULT_OPERATOR_ID, limit=1)
    assert listed[0].status == PERSONA_CURIOSITY_STATUS_ANSWERED
    assert listed[0].response_turn_id == "turn-2"


@pytest.mark.asyncio
async def test_sa_persona_curiosity_delete_created_since_scopes_by_conversation(
    session_factory: sessionmaker,
) -> None:
    """Turn-undo's window delete: only the attempt in *this* conversation
    at-or-after ``since`` goes — an earlier attempt and one recorded in a
    different conversation both survive."""
    await _ensure_operator(session_factory)
    await _ensure_character(session_factory, "char-A")
    repo = SAPersonaCuriosityRepository(session_factory)

    stale = await repo.add(
        PersonaCuriosityAttempt.new(
            character_id="char-A",
            operator_id=DEFAULT_OPERATOR_ID,
            surface=PERSONA_CURIOSITY_SURFACE_CHAT,
            target_layer=1,
            target_topic="stale",
            question_intent="predates the turn",
            created_at=_NOW - timedelta(hours=1),
            conversation_id="conv-1",
        ),
    )
    this_turn = await repo.add(
        PersonaCuriosityAttempt.new(
            character_id="char-A",
            operator_id=DEFAULT_OPERATOR_ID,
            surface=PERSONA_CURIOSITY_SURFACE_CHAT,
            target_layer=2,
            target_topic="this turn",
            question_intent="asked this turn",
            created_at=_NOW,
            conversation_id="conv-1",
        ),
    )
    other_conversation = await repo.add(
        PersonaCuriosityAttempt.new(
            character_id="char-A",
            operator_id=DEFAULT_OPERATOR_ID,
            surface=PERSONA_CURIOSITY_SURFACE_CHAT,
            target_layer=3,
            target_topic="other conversation",
            question_intent="asked elsewhere",
            created_at=_NOW,
            conversation_id="conv-2",
        ),
    )

    deleted = await repo.delete_created_since("char-A", "conv-1", _NOW)

    assert deleted == 1
    remaining_ids = {
        a.id for a in await repo.list_recent("char-A", DEFAULT_OPERATOR_ID, limit=10)
    }
    assert stale.id in remaining_ids
    assert other_conversation.id in remaining_ids
    assert this_turn.id not in remaining_ids
