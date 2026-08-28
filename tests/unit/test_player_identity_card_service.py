"""IC1 — card CRUD over the in-memory repository.

Covers what the routes delegate: the per-operator cap, same-name save
semantics (refuse, or overwrite in place), rename conflicts, and the rule
that every lookup is scoped to its owner.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from kokoro_link.application.services.player_identity_card_service import (
    PlayerIdentityCardLimitReachedError,
    PlayerIdentityCardNameConflictError,
    PlayerIdentityCardService,
)
from kokoro_link.domain.entities.player_identity_card import (
    PLAYER_IDENTITY_CARDS_PER_OPERATOR,
    PlayerIdentityCard,
)
from kokoro_link.infrastructure.repositories.in_memory_player_identity_cards import (
    InMemoryPlayerIdentityCardRepository,
)


def _service(limit: int = PLAYER_IDENTITY_CARDS_PER_OPERATOR) -> tuple[
    PlayerIdentityCardService, InMemoryPlayerIdentityCardRepository,
]:
    repository = InMemoryPlayerIdentityCardRepository()
    return PlayerIdentityCardService(repository, limit=limit), repository


@pytest.mark.asyncio
async def test_save_then_list_and_get() -> None:
    service, _ = _service()

    saved = await service.save_card(
        operator_id="alice",
        name="上班族的我",
        known_context="同一間事務所",
        persona_note="我是超能力者",
    )

    listed = await service.list_cards("alice")
    assert [card.id for card in listed] == [saved.id]
    assert listed[0].known_context == "同一間事務所"
    assert listed[0].persona_note == "我是超能力者"

    fetched = await service.get_card(card_id=saved.id, operator_id="alice")
    assert fetched is not None and fetched.name == "上班族的我"


@pytest.mark.asyncio
async def test_list_is_newest_update_first() -> None:
    service, _ = _service()
    first = await service.save_card(
        operator_id="alice",
        name="一號",
        now=datetime(2026, 8, 20, tzinfo=timezone.utc),
    )
    second = await service.save_card(
        operator_id="alice",
        name="二號",
        now=datetime(2026, 8, 26, tzinfo=timezone.utc),
    )

    assert [card.id for card in await service.list_cards("alice")] == [
        second.id, first.id,
    ]


@pytest.mark.asyncio
async def test_cards_are_scoped_to_their_owner() -> None:
    service, _ = _service()
    alice_card = await service.save_card(operator_id="alice", name="上班族的我")

    assert await service.list_cards("bob") == []
    assert await service.get_card(card_id=alice_card.id, operator_id="bob") is None
    assert await service.delete_card(
        card_id=alice_card.id, operator_id="bob",
    ) is False
    assert await service.rename_card(
        card_id=alice_card.id, operator_id="bob", name="偷改",
    ) is None
    # Bob's failed attempts left Alice's card untouched.
    assert await service.get_card(
        card_id=alice_card.id, operator_id="alice",
    ) is not None


@pytest.mark.asyncio
async def test_same_name_without_overwrite_conflicts() -> None:
    service, _ = _service()
    first = await service.save_card(
        operator_id="alice", name="上班族的我", known_context="原本的",
    )

    with pytest.raises(PlayerIdentityCardNameConflictError) as raised:
        await service.save_card(
            operator_id="alice", name="  上班族的我  ", known_context="新的",
        )

    assert raised.value.existing.id == first.id
    stored = await service.get_card(card_id=first.id, operator_id="alice")
    assert stored is not None and stored.known_context == "原本的"


@pytest.mark.asyncio
async def test_the_double_reports_a_name_race_the_way_the_database_does() -> None:
    """Replica pin for the double's stand-in unique constraint.

    Both repositories are written against the same port, so the error a
    lost ``(operator_id, name)`` race produces has to be the same class
    in both — otherwise every service and route test that runs on the
    double proves a 409 the production adapter would have served as a
    500.
    """
    repository = InMemoryPlayerIdentityCardRepository()
    winner = PlayerIdentityCard.create(operator_id="alice", name="上班族的我")
    await repository.upsert(winner)
    loser = PlayerIdentityCard.create(
        operator_id="alice", name="上班族的我", known_context="新的",
    )
    assert loser.id != winner.id

    with pytest.raises(PlayerIdentityCardNameConflictError) as raised:
        await repository.upsert(loser)

    assert raised.value.existing.id == winner.id
    assert await repository.count_for_operator("alice") == 1


@pytest.mark.asyncio
async def test_overwrite_replaces_content_and_keeps_the_id() -> None:
    service, _ = _service()
    created = datetime(2026, 8, 20, tzinfo=timezone.utc)
    later = datetime(2026, 8, 27, tzinfo=timezone.utc)
    first = await service.save_card(
        operator_id="alice",
        name="上班族的我",
        known_context="原本的",
        persona_note="原人設",
        now=created,
    )

    overwritten = await service.save_card(
        operator_id="alice",
        name="上班族的我",
        overwrite=True,
        known_context="新的",
        persona_note="新人設",
        now=later,
    )

    assert overwritten.id == first.id
    assert overwritten.created_at == created
    assert overwritten.updated_at == later
    assert overwritten.known_context == "新的"
    assert overwritten.persona_note == "新人設"
    assert len(await service.list_cards("alice")) == 1


@pytest.mark.asyncio
async def test_different_operators_may_share_a_card_name() -> None:
    service, _ = _service()
    await service.save_card(operator_id="alice", name="上班族的我")

    bob = await service.save_card(operator_id="bob", name="上班族的我")

    assert bob.name == "上班族的我"
    assert len(await service.list_cards("alice")) == 1


@pytest.mark.asyncio
async def test_new_card_past_the_limit_is_refused() -> None:
    service, _ = _service(limit=3)
    for index in range(3):
        await service.save_card(operator_id="alice", name=f"卡{index}")

    with pytest.raises(PlayerIdentityCardLimitReachedError) as raised:
        await service.save_card(operator_id="alice", name="第四張")

    assert (raised.value.current, raised.value.limit) == (3, 3)
    assert len(await service.list_cards("alice")) == 3


@pytest.mark.asyncio
async def test_overwrite_is_allowed_at_the_limit() -> None:
    """An overwrite replaces a row; it does not add one."""
    service, _ = _service(limit=2)
    await service.save_card(operator_id="alice", name="卡0")
    await service.save_card(operator_id="alice", name="卡1")

    overwritten = await service.save_card(
        operator_id="alice", name="卡1", overwrite=True, known_context="新的",
    )

    assert overwritten.known_context == "新的"
    assert len(await service.list_cards("alice")) == 2


@pytest.mark.asyncio
async def test_the_limit_is_per_operator() -> None:
    service, _ = _service(limit=1)
    await service.save_card(operator_id="alice", name="卡0")

    assert await service.save_card(operator_id="bob", name="卡0") is not None


@pytest.mark.asyncio
async def test_rename_changes_only_the_name() -> None:
    service, _ = _service()
    card = await service.save_card(
        operator_id="alice", name="上班族的我", known_context="同一間事務所",
    )

    renamed = await service.rename_card(
        card_id=card.id, operator_id="alice", name="社畜的我",
    )

    assert renamed is not None
    assert renamed.id == card.id
    assert renamed.name == "社畜的我"
    assert renamed.known_context == "同一間事務所"
    assert [c.name for c in await service.list_cards("alice")] == ["社畜的我"]


@pytest.mark.asyncio
async def test_rename_onto_an_existing_name_conflicts() -> None:
    service, _ = _service()
    first = await service.save_card(operator_id="alice", name="上班族的我")
    second = await service.save_card(operator_id="alice", name="勇者的我")

    with pytest.raises(PlayerIdentityCardNameConflictError) as raised:
        await service.rename_card(
            card_id=second.id, operator_id="alice", name="上班族的我",
        )

    assert raised.value.existing.id == first.id
    stored = await service.get_card(card_id=second.id, operator_id="alice")
    assert stored is not None and stored.name == "勇者的我"


@pytest.mark.asyncio
async def test_renaming_a_card_to_its_own_name_is_not_a_conflict() -> None:
    service, _ = _service()
    card = await service.save_card(operator_id="alice", name="上班族的我")

    renamed = await service.rename_card(
        card_id=card.id, operator_id="alice", name="上班族的我",
    )

    assert renamed is not None and renamed.name == "上班族的我"


@pytest.mark.asyncio
async def test_delete_removes_the_card_and_frees_its_name() -> None:
    service, _ = _service()
    card = await service.save_card(operator_id="alice", name="上班族的我")

    assert await service.delete_card(card_id=card.id, operator_id="alice") is True
    assert await service.delete_card(card_id=card.id, operator_id="alice") is False
    assert await service.list_cards("alice") == []

    reused = await service.save_card(operator_id="alice", name="上班族的我")
    assert reused.id != card.id
