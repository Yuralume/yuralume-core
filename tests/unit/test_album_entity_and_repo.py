"""BDD for ``AlbumItem`` entity + ``InMemoryAlbumRepository``.

Kept together because the repo has almost no logic of its own — the
interesting invariants (factory defaults, source enum, tz-awareness,
newest-first ordering) are all entity / repo behaviour any SA impl
will inherit through the port.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.domain.entities.album_item import (
    SOURCE_STAGE,
    SOURCE_TOOL,
    AlbumItem,
)
from kokoro_link.infrastructure.repositories.in_memory_album import (
    InMemoryAlbumRepository,
)


# ---------- entity ----------


def test_create_assigns_id_and_defaults_created_at() -> None:
    item = AlbumItem.create(
        character_id="char-1",
        url="/uploads/characters/char-1/tools/x.png",
        source=SOURCE_TOOL,
    )
    assert item.id
    assert len(item.id) >= 16
    assert item.created_at.tzinfo is not None


def test_rejects_unknown_source() -> None:
    with pytest.raises(ValueError, match="source"):
        AlbumItem.create(
            character_id="c", url="/uploads/a.png", source="mystery",
        )


def test_rejects_empty_character_id() -> None:
    with pytest.raises(ValueError, match="character_id"):
        AlbumItem.create(
            character_id="", url="/uploads/a.png", source=SOURCE_TOOL,
        )


def test_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError, match="timezone"):
        AlbumItem(
            id="x",
            character_id="c",
            url="/u/a.png",
            source=SOURCE_TOOL,
            created_at=datetime(2026, 1, 1),
        )


def test_rejects_negative_byte_size() -> None:
    with pytest.raises(ValueError, match="byte_size"):
        AlbumItem.create(
            character_id="c",
            url="/u/a.png",
            source=SOURCE_TOOL,
            byte_size=-1,
        )


# ---------- repo ----------


@pytest.fixture
def repo() -> InMemoryAlbumRepository:
    return InMemoryAlbumRepository()


@pytest.mark.asyncio
async def test_add_then_get(repo: InMemoryAlbumRepository) -> None:
    item = AlbumItem.create(
        character_id="char-1", url="/u/a.png", source=SOURCE_TOOL,
    )
    await repo.add(item)
    assert await repo.get(item.id) == item


@pytest.mark.asyncio
async def test_list_newest_first(repo: InMemoryAlbumRepository) -> None:
    base = datetime(2026, 4, 20, 12, 0, tzinfo=timezone.utc)
    old = AlbumItem.create(
        character_id="c", url="/u/old.png", source=SOURCE_TOOL,
        created_at=base,
    )
    new = AlbumItem.create(
        character_id="c", url="/u/new.png", source=SOURCE_TOOL,
        created_at=base + timedelta(hours=1),
    )
    await repo.add(old)
    await repo.add(new)

    got = await repo.list_for_character("c")
    assert [i.url for i in got] == ["/u/new.png", "/u/old.png"]


@pytest.mark.asyncio
async def test_list_is_character_scoped(repo: InMemoryAlbumRepository) -> None:
    await repo.add(AlbumItem.create(
        character_id="alice", url="/u/a.png", source=SOURCE_TOOL,
    ))
    await repo.add(AlbumItem.create(
        character_id="bob", url="/u/b.png", source=SOURCE_TOOL,
    ))
    alice = await repo.list_for_character("alice")
    assert [i.url for i in alice] == ["/u/a.png"]


@pytest.mark.asyncio
async def test_list_with_limit_and_offset(
    repo: InMemoryAlbumRepository,
) -> None:
    base = datetime(2026, 4, 20, 12, 0, tzinfo=timezone.utc)
    for idx in range(5):
        await repo.add(AlbumItem.create(
            character_id="c",
            url=f"/u/{idx}.png",
            source=SOURCE_TOOL,
            created_at=base + timedelta(minutes=idx),
        ))
    # newest-first: 4, 3, 2, 1, 0
    page = await repo.list_for_character("c", limit=2, offset=1)
    assert [i.url for i in page] == ["/u/3.png", "/u/2.png"]


@pytest.mark.asyncio
async def test_count(repo: InMemoryAlbumRepository) -> None:
    for idx in range(3):
        await repo.add(AlbumItem.create(
            character_id="c",
            url=f"/u/{idx}.png",
            source=SOURCE_TOOL,
        ))
    assert await repo.count_for_character("c") == 3
    assert await repo.count_for_character("empty") == 0


@pytest.mark.asyncio
async def test_delete_returns_true_and_removes(
    repo: InMemoryAlbumRepository,
) -> None:
    item = AlbumItem.create(
        character_id="c", url="/u/a.png", source=SOURCE_TOOL,
    )
    await repo.add(item)
    assert await repo.delete(item.id) is True
    assert await repo.get(item.id) is None
    assert await repo.list_for_character("c") == []


@pytest.mark.asyncio
async def test_delete_unknown_returns_false(
    repo: InMemoryAlbumRepository,
) -> None:
    assert await repo.delete("does-not-exist") is False


@pytest.mark.asyncio
async def test_add_duplicate_id_is_rejected(
    repo: InMemoryAlbumRepository,
) -> None:
    item = AlbumItem.create(
        character_id="c", url="/u/a.png", source=SOURCE_TOOL,
    )
    await repo.add(item)
    with pytest.raises(ValueError, match="already exists"):
        await repo.add(item)


@pytest.mark.asyncio
async def test_delete_for_character_cascades(
    repo: InMemoryAlbumRepository,
) -> None:
    for idx in range(3):
        await repo.add(AlbumItem.create(
            character_id="c", url=f"/u/{idx}.png", source=SOURCE_TOOL,
        ))
    await repo.add(AlbumItem.create(
        character_id="other", url="/u/keep.png", source=SOURCE_STAGE,
    ))

    removed = await repo.delete_for_character("c")
    assert removed == 3
    assert await repo.list_for_character("c") == []
    # Other character untouched
    other = await repo.list_for_character("other")
    assert [i.url for i in other] == ["/u/keep.png"]
