"""Integration tests for ``SAAlbumRepository`` against PostgreSQL.

Same philosophy as ``test_sa_repositories.py``: exercise ordering +
cascade + tz round-trip against a real engine so subtle SA / asyncpg /
pgvector quirks show up here rather than in production.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import sessionmaker  # noqa: F401 — type alias for fixtures

from kokoro_link.domain.entities.album_item import SOURCE_STAGE, SOURCE_TOOL, AlbumItem
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.persistence.sa_album_repository import (
    SAAlbumRepository,
)
from kokoro_link.infrastructure.persistence.sa_character_repository import (
    SACharacterRepository,
)


def _default_state() -> CharacterState:
    return CharacterState(
        emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
    )


async def _seed_character(session_factory: sessionmaker, name: str = "Alice") -> Character:
    repo = SACharacterRepository(session_factory)
    character = Character.create(
        name=name,
        summary="",
        personality=[],
        interests=[],
        speaking_style="",
        boundaries=[],
        state=_default_state(),
    )
    await repo.save(character)
    return character


@pytest.mark.asyncio
async def test_album_add_list_newest_first(session_factory: sessionmaker) -> None:
    character = await _seed_character(session_factory)
    repo = SAAlbumRepository(session_factory)

    base = datetime(2026, 4, 20, 12, 0, tzinfo=timezone.utc)
    for idx, offset in enumerate([0, 2, 1]):
        await repo.add(AlbumItem.create(
            character_id=character.id,
            url=f"/uploads/characters/{character.id}/tools/{idx}.png",
            source=SOURCE_TOOL,
            created_at=base + timedelta(minutes=offset),
        ))

    items = await repo.list_for_character(character.id)
    # Expect order by created_at DESC: idx=1 (t+2), idx=2 (t+1), idx=0 (t+0)
    assert [item.url.rsplit("/", 1)[-1] for item in items] == [
        "1.png", "2.png", "0.png",
    ]
    # Every created_at must survive as tz-aware
    assert all(item.created_at.tzinfo is not None for item in items)


@pytest.mark.asyncio
async def test_album_count_and_delete(session_factory: sessionmaker) -> None:
    character = await _seed_character(session_factory, name="Bob")
    repo = SAAlbumRepository(session_factory)

    items = []
    for idx in range(3):
        item = AlbumItem.create(
            character_id=character.id,
            url=f"/uploads/characters/{character.id}/tools/{idx}.png",
            source=SOURCE_TOOL,
        )
        await repo.add(item)
        items.append(item)

    assert await repo.count_for_character(character.id) == 3
    assert await repo.delete(items[0].id) is True
    assert await repo.count_for_character(character.id) == 2
    assert await repo.delete("does-not-exist") is False


@pytest.mark.asyncio
async def test_album_cascade_deletes_with_character(
    session_factory: sessionmaker,
) -> None:
    character = await _seed_character(session_factory, name="Charlie")
    album_repo = SAAlbumRepository(session_factory)
    char_repo = SACharacterRepository(session_factory)

    for idx in range(2):
        await album_repo.add(AlbumItem.create(
            character_id=character.id,
            url=f"/u/{idx}.png",
            source=SOURCE_TOOL,
        ))

    assert await album_repo.count_for_character(character.id) == 2

    # Character delete cascades album rows via ``ondelete=CASCADE``
    await char_repo.delete(character.id)
    assert await album_repo.count_for_character(character.id) == 0


@pytest.mark.asyncio
async def test_album_delete_for_character_returns_count(
    session_factory: sessionmaker,
) -> None:
    character = await _seed_character(session_factory, name="Dana")
    repo = SAAlbumRepository(session_factory)

    for idx in range(5):
        await repo.add(AlbumItem.create(
            character_id=character.id,
            url=f"/u/{idx}.png",
            source=SOURCE_STAGE if idx % 2 else SOURCE_TOOL,
        ))

    assert await repo.delete_for_character(character.id) == 5
    assert await repo.count_for_character(character.id) == 0
    # Idempotent — deleting an empty set returns 0
    assert await repo.delete_for_character(character.id) == 0
