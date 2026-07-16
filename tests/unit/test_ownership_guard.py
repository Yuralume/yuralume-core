"""Service-layer ownership guard for per-user character isolation."""

from __future__ import annotations

import pytest

from kokoro_link.application.exceptions import CharacterNotOwned
from kokoro_link.application.services.ownership import ensure_character_owned
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)


def _make_character(user_id: str = "default", char_id: str = "char-1") -> Character:
    return Character(
        id=char_id,
        name="Test",
        summary="",
        user_id=user_id,
        personality=[],
        interests=[],
        speaking_style="",
        boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
    )


@pytest.mark.asyncio
async def test_owner_resolves_to_character() -> None:
    repo = InMemoryCharacterRepository()
    char = _make_character(user_id="alice")
    await repo.save(char)
    resolved = await ensure_character_owned(repo, char.id, "alice")
    assert resolved.id == char.id


@pytest.mark.asyncio
async def test_wrong_owner_raises_not_owned() -> None:
    repo = InMemoryCharacterRepository()
    await repo.save(_make_character(user_id="alice", char_id="alice-char"))
    with pytest.raises(CharacterNotOwned) as exc_info:
        await ensure_character_owned(repo, "alice-char", "bob")
    assert exc_info.value.character_id == "alice-char"


@pytest.mark.asyncio
async def test_missing_character_raises_not_owned() -> None:
    """We do NOT distinguish 'not found' from 'not yours' — both
    surface as the same exception to prevent enumeration."""
    repo = InMemoryCharacterRepository()
    with pytest.raises(CharacterNotOwned):
        await ensure_character_owned(repo, "ghost", "alice")


@pytest.mark.asyncio
async def test_list_for_user_filters_correctly() -> None:
    repo = InMemoryCharacterRepository()
    await repo.save(_make_character(user_id="alice", char_id="a1"))
    await repo.save(_make_character(user_id="alice", char_id="a2"))
    await repo.save(_make_character(user_id="bob", char_id="b1"))

    alice_chars = await repo.list_for_user("alice")
    bob_chars = await repo.list_for_user("bob")
    ghost_chars = await repo.list_for_user("ghost")

    assert {c.id for c in alice_chars} == {"a1", "a2"}
    assert {c.id for c in bob_chars} == {"b1"}
    assert ghost_chars == []
