"""IC1 — SQLAlchemy round trip for 玩家身分卡."""

from datetime import datetime, timezone

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from kokoro_link.contracts.player_identity_card import (
    PlayerIdentityCardNameConflictError,
)
from kokoro_link.domain.entities.operator_profile import (
    DEFAULT_OPERATOR_ID,
    OperatorProfile,
)
from kokoro_link.domain.entities.player_identity_card import PlayerIdentityCard
from kokoro_link.infrastructure.persistence.sa_operator_profile_repository import (
    SAOperatorProfileRepository,
)
from kokoro_link.infrastructure.persistence.sa_player_identity_card_repository import (
    SAPlayerIdentityCardRepository,
)


async def _operators(session_factory: sessionmaker) -> None:
    repo = SAOperatorProfileRepository(session_factory)
    if await repo.get_default() is None:
        await repo.save(
            OperatorProfile(id=DEFAULT_OPERATOR_ID, display_name="艾力"),
        )
    if await repo.get("other") is None:
        await repo.save(OperatorProfile(id="other", display_name="別人"))


@pytest.mark.asyncio
async def test_upsert_get_list_and_delete(session_factory: sessionmaker) -> None:
    await _operators(session_factory)
    repo = SAPlayerIdentityCardRepository(session_factory)
    stamped = datetime(2026, 8, 27, 9, 0, tzinfo=timezone.utc)
    card = PlayerIdentityCard.create(
        operator_id=DEFAULT_OPERATOR_ID,
        name="上班族的我",
        now=stamped,
        relationship_label="同事",
        known_context="同一間事務所",
        schedule_involvement_policy="invite_required",
        proactive_permission=True,
        persona_note="我是超能力者",
    )

    assert await repo.get(
        card_id=card.id, operator_id=DEFAULT_OPERATOR_ID,
    ) is None
    assert await repo.count_for_operator(DEFAULT_OPERATOR_ID) == 0

    await repo.upsert(card)

    loaded = await repo.get(card_id=card.id, operator_id=DEFAULT_OPERATOR_ID)
    assert loaded is not None
    assert loaded.name == "上班族的我"
    assert loaded.relationship_label == "同事"
    assert loaded.known_context == "同一間事務所"
    assert loaded.schedule_involvement_policy == "invite_required"
    assert loaded.proactive_permission is True
    assert loaded.persona_note == "我是超能力者"
    assert loaded.created_at is not None and loaded.created_at.tzinfo is not None
    assert loaded.updated_at is not None and loaded.updated_at.tzinfo is not None

    assert await repo.count_for_operator(DEFAULT_OPERATOR_ID) == 1
    assert [item.id for item in await repo.list_for_operator(
        DEFAULT_OPERATOR_ID,
    )] == [card.id]

    assert await repo.delete(
        card_id=card.id, operator_id=DEFAULT_OPERATOR_ID,
    ) is True
    assert await repo.delete(
        card_id=card.id, operator_id=DEFAULT_OPERATOR_ID,
    ) is False


@pytest.mark.asyncio
async def test_overwrite_keeps_the_id_and_created_at(
    session_factory: sessionmaker,
) -> None:
    await _operators(session_factory)
    repo = SAPlayerIdentityCardRepository(session_factory)
    created = datetime(2026, 8, 20, 9, 0, tzinfo=timezone.utc)
    later = datetime(2026, 8, 27, 9, 0, tzinfo=timezone.utc)
    card = PlayerIdentityCard.create(
        operator_id=DEFAULT_OPERATOR_ID,
        name="上班族的我",
        now=created,
        known_context="原本的",
    )
    await repo.upsert(card)

    replacement = PlayerIdentityCard.create(
        operator_id=DEFAULT_OPERATOR_ID, name="上班族的我", known_context="新的",
    )
    await repo.upsert(card.overwritten_by(replacement, now=later))

    reloaded = await repo.get(card_id=card.id, operator_id=DEFAULT_OPERATOR_ID)
    assert reloaded is not None
    assert reloaded.known_context == "新的"
    assert reloaded.created_at == created
    assert reloaded.updated_at == later
    assert await repo.count_for_operator(DEFAULT_OPERATOR_ID) == 1


@pytest.mark.asyncio
async def test_losing_the_name_race_is_a_typed_conflict_not_an_integrity_error(
    session_factory: sessionmaker,
) -> None:
    """Two distinct ids, one name: the DB's refusal must arrive typed.

    This is the concurrency path the service's ``find_by_name``
    pre-check cannot cover — both savers read "name free" before either
    insert. If the raw ``IntegrityError`` escaped, the loser would get a
    500 (with a driver traceback) for what is an ordinary "that name is
    taken, overwrite?".
    """
    await _operators(session_factory)
    repo = SAPlayerIdentityCardRepository(session_factory)
    winner = PlayerIdentityCard.create(
        operator_id=DEFAULT_OPERATOR_ID, name="上班族的我",
    )
    await repo.upsert(winner)
    loser = PlayerIdentityCard.create(
        operator_id=DEFAULT_OPERATOR_ID, name="上班族的我", known_context="新的",
    )
    assert loser.id != winner.id

    with pytest.raises(PlayerIdentityCardNameConflictError) as raised:
        await repo.upsert(loser)

    # The error points at the card that won, so the API can answer 409
    # with an id the player can actually overwrite.
    assert raised.value.existing.id == winner.id
    assert await repo.count_for_operator(DEFAULT_OPERATOR_ID) == 1
    stored = await repo.get(card_id=winner.id, operator_id=DEFAULT_OPERATOR_ID)
    assert stored is not None and stored.known_context == ""


@pytest.mark.asyncio
async def test_a_foreign_key_violation_is_not_disguised_as_a_name_conflict(
    session_factory: sessionmaker,
) -> None:
    """Only ``(operator_id, name)`` gets translated; other integrity
    failures stay the defects they are."""
    await _operators(session_factory)
    repo = SAPlayerIdentityCardRepository(session_factory)

    with pytest.raises(IntegrityError):
        await repo.upsert(
            PlayerIdentityCard.create(
                operator_id="no-such-operator", name="上班族的我",
            ),
        )


@pytest.mark.asyncio
async def test_cards_are_isolated_per_operator(
    session_factory: sessionmaker,
) -> None:
    await _operators(session_factory)
    repo = SAPlayerIdentityCardRepository(session_factory)
    mine = PlayerIdentityCard.create(
        operator_id=DEFAULT_OPERATOR_ID, name="上班族的我",
    )
    await repo.upsert(mine)

    assert await repo.get(card_id=mine.id, operator_id="other") is None
    assert await repo.list_for_operator("other") == []
    assert await repo.count_for_operator("other") == 0
    assert await repo.find_by_name(
        operator_id="other", name="上班族的我",
    ) is None
    assert await repo.delete(card_id=mine.id, operator_id="other") is False

    # Same name under a different account is allowed.
    theirs = PlayerIdentityCard.create(operator_id="other", name="上班族的我")
    await repo.upsert(theirs)
    assert await repo.count_for_operator("other") == 1
    assert await repo.get(card_id=mine.id, operator_id=DEFAULT_OPERATOR_ID) is not None


@pytest.mark.asyncio
async def test_list_is_newest_update_first(
    session_factory: sessionmaker,
) -> None:
    await _operators(session_factory)
    repo = SAPlayerIdentityCardRepository(session_factory)
    older = PlayerIdentityCard.create(
        operator_id=DEFAULT_OPERATOR_ID,
        name="舊的",
        now=datetime(2026, 8, 20, tzinfo=timezone.utc),
    )
    newer = PlayerIdentityCard.create(
        operator_id=DEFAULT_OPERATOR_ID,
        name="新的",
        now=datetime(2026, 8, 26, tzinfo=timezone.utc),
    )
    await repo.upsert(older)
    await repo.upsert(newer)

    assert [
        card.id for card in await repo.list_for_operator(DEFAULT_OPERATOR_ID)
    ] == [newer.id, older.id]


@pytest.mark.asyncio
async def test_find_by_name_matches_the_trimmed_name(
    session_factory: sessionmaker,
) -> None:
    await _operators(session_factory)
    repo = SAPlayerIdentityCardRepository(session_factory)
    card = PlayerIdentityCard.create(
        operator_id=DEFAULT_OPERATOR_ID, name="上班族的我",
    )
    await repo.upsert(card)

    found = await repo.find_by_name(
        operator_id=DEFAULT_OPERATOR_ID, name="  上班族的我  ",
    )

    assert found is not None and found.id == card.id
    assert await repo.find_by_name(
        operator_id=DEFAULT_OPERATOR_ID, name="沒有這張",
    ) is None
    assert await repo.find_by_name(
        operator_id=DEFAULT_OPERATOR_ID, name="   ",
    ) is None
