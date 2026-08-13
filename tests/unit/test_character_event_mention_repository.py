"""Contract tests for the character event-mention repository.

Two backends run the *same* assertions:

- ``memory`` — the DB-less dev / unit twin.
- ``sqlite`` — the real SQLAlchemy adapter against ``sqlite+aiosqlite``.

The SQLite leg is load-bearing here: the write is an upsert, and a
``postgresql``-dialect ``ON CONFLICT`` compiles fine on Postgres and blows
up on the self-host database. Self-host gets the SQL adapter too — the
container swaps purely on "is a database wired".
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from kokoro_link.domain.entities.character_event_mention import (
    CharacterEventMention,
)
from kokoro_link.infrastructure.persistence.engine import build_session_factory
from kokoro_link.infrastructure.persistence.rss_models import (
    CharacterEventMentionRow,
)
from kokoro_link.infrastructure.persistence.sa_character_event_mention_repository import (  # noqa: E501
    SaCharacterEventMentionRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_character_event_mentions import (  # noqa: E501
    InMemoryCharacterEventMentionRepository,
)

UTC = timezone.utc
_NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)


@pytest_asyncio.fixture(params=["memory", "sqlite"])
async def repository(request):  # noqa: ANN001, ANN201
    if request.param == "memory":
        yield InMemoryCharacterEventMentionRepository()
        return
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(CharacterEventMentionRow.__table__.create)
    try:
        yield SaCharacterEventMentionRepository(build_session_factory(engine))
    finally:
        await engine.dispose()


def _mention(
    *,
    character_id: str = "char-1",
    world_event_id: str = "we-1",
    surface: str = "proactive_message",
    mentioned_at: datetime = _NOW,
) -> CharacterEventMention:
    return CharacterEventMention.create(
        character_id=character_id,
        world_event_id=world_event_id,
        surface=surface,
        mentioned_at=mentioned_at,
    )


@pytest.mark.asyncio
async def test_unknown_character_has_no_mentions(repository) -> None:  # noqa: ANN001
    assert await repository.list_recent("nobody") == []


@pytest.mark.asyncio
async def test_records_and_reads_back(repository) -> None:  # noqa: ANN001
    await repository.record(_mention())
    rows = await repository.list_recent("char-1")
    assert len(rows) == 1
    assert rows[0].world_event_id == "we-1"
    assert rows[0].surface == "proactive_message"
    assert rows[0].mentioned_at == _NOW


@pytest.mark.asyncio
async def test_repeat_mention_refreshes_instead_of_duplicating(
    repository,  # noqa: ANN001
) -> None:
    await repository.record(_mention(mentioned_at=_NOW - timedelta(days=1)))
    await repository.record(_mention(surface="feed_post", mentioned_at=_NOW))
    rows = await repository.list_recent("char-1")
    assert len(rows) == 1
    assert rows[0].mentioned_at == _NOW
    assert rows[0].surface == "feed_post"


@pytest.mark.asyncio
async def test_newest_first_and_limited(repository) -> None:  # noqa: ANN001
    for index in range(4):
        await repository.record(
            _mention(
                world_event_id=f"we-{index}",
                mentioned_at=_NOW - timedelta(hours=index),
            ),
        )
    rows = await repository.list_recent("char-1", limit=2)
    assert [row.world_event_id for row in rows] == ["we-0", "we-1"]


@pytest.mark.asyncio
async def test_non_positive_limit_reads_nothing(repository) -> None:  # noqa: ANN001
    await repository.record(_mention())
    assert await repository.list_recent("char-1", limit=0) == []


@pytest.mark.asyncio
async def test_mentions_are_scoped_per_character(repository) -> None:  # noqa: ANN001
    await repository.record(_mention(character_id="char-1"))
    await repository.record(_mention(character_id="char-2"))
    assert len(await repository.list_recent("char-1")) == 1
    assert len(await repository.list_recent("char-2")) == 1


@pytest.mark.asyncio
async def test_delete_for_character_purges_only_that_character(
    repository,  # noqa: ANN001
) -> None:
    await repository.record(_mention(character_id="char-1"))
    await repository.record(_mention(character_id="char-2"))
    assert await repository.delete_for_character("char-1") == 1
    assert await repository.list_recent("char-1") == []
    assert len(await repository.list_recent("char-2")) == 1


@pytest.mark.asyncio
async def test_delete_older_than_bounds_the_table(repository) -> None:  # noqa: ANN001
    await repository.record(
        _mention(world_event_id="old", mentioned_at=_NOW - timedelta(days=40)),
    )
    await repository.record(_mention(world_event_id="fresh", mentioned_at=_NOW))
    deleted = await repository.delete_older_than(_NOW - timedelta(days=30))
    assert deleted == 1
    rows = await repository.list_recent("char-1")
    assert [row.world_event_id for row in rows] == ["fresh"]


@pytest.mark.asyncio
async def test_blank_identifiers_are_rejected_at_construction() -> None:
    with pytest.raises(ValueError):
        CharacterEventMention.create(
            character_id=" ", world_event_id="we-1",
            surface="proactive_message", mentioned_at=_NOW,
        )
    with pytest.raises(ValueError):
        CharacterEventMention.create(
            character_id="char-1", world_event_id="",
            surface="proactive_message", mentioned_at=_NOW,
        )
    with pytest.raises(ValueError):
        CharacterEventMention.create(
            character_id="char-1", world_event_id="we-1",
            surface="", mentioned_at=_NOW,
        )
