"""BDD for ``FeedPost`` entity + ``InMemoryFeedPostRepository``.

Same shape as ``test_album_entity_and_repo`` — entity invariants live
beside the repo so the SA implementation inherits the contract via
the port. Only Phase 1 behaviour: list pagination, dedup-by-source,
daily-count, latest_for_character.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from kokoro_link.domain.entities.feed_post import (
    FeedPost,
    FeedReactionSummary,
)
from kokoro_link.domain.value_objects.feed_kind import FeedKind
from kokoro_link.domain.value_objects.feed_source import FeedSource
from kokoro_link.infrastructure.repositories.in_memory_feed_posts import (
    InMemoryFeedPostRepository,
)


# ---------- entity ----------


def test_create_assigns_id_and_defaults() -> None:
    post = FeedPost.create(
        character_id="char-1",
        kind=FeedKind.MOOD,
        content_text="今天的咖啡好香。",
        source=FeedSource.silence(),
    )
    assert post.id and len(post.id) >= 16
    assert post.created_at.tzinfo is not None
    assert post.image_url is None
    assert post.reactions == FeedReactionSummary(likes=0, comments=0)


def test_rejects_empty_content() -> None:
    with pytest.raises(ValueError, match="content_text"):
        FeedPost.create(
            character_id="c",
            kind=FeedKind.MOOD,
            content_text="   ",
            source=FeedSource.silence(),
        )


def test_rejects_empty_character_id() -> None:
    with pytest.raises(ValueError, match="character_id"):
        FeedPost.create(
            character_id="",
            kind=FeedKind.MOOD,
            content_text="x",
            source=FeedSource.silence(),
        )


def test_rejects_empty_image_url_string() -> None:
    with pytest.raises(ValueError, match="image_url"):
        FeedPost.create(
            character_id="c",
            kind=FeedKind.MOOD,
            content_text="x",
            source=FeedSource.silence(),
            image_url="   ",
        )


def test_kind_accepts_string_via_factory() -> None:
    post = FeedPost.create(
        character_id="c",
        kind="reflection",
        content_text="…",
        source=FeedSource.silence(),
    )
    assert post.kind == FeedKind.REFLECTION


def test_with_fields_clears_image_explicitly() -> None:
    post = FeedPost.create(
        character_id="c",
        kind=FeedKind.MOOD,
        content_text="…",
        source=FeedSource.silence(),
        image_url="/uploads/feed/c/x.png",
    )
    cleared = post.with_fields(image_url=None)
    assert cleared.image_url is None


def test_with_fields_leaves_image_alone_by_default() -> None:
    post = FeedPost.create(
        character_id="c",
        kind=FeedKind.MOOD,
        content_text="abc",
        source=FeedSource.silence(),
        image_url="/uploads/feed/c/x.png",
    )
    updated = post.with_fields(content_text="新內容")
    assert updated.content_text == "新內容"
    assert updated.image_url == "/uploads/feed/c/x.png"


# ---------- repo ----------


@pytest.fixture
def repo() -> InMemoryFeedPostRepository:
    return InMemoryFeedPostRepository()


@pytest.mark.asyncio
async def test_add_then_get(repo: InMemoryFeedPostRepository) -> None:
    post = FeedPost.create(
        character_id="char-1",
        kind=FeedKind.MOOD,
        content_text="hi",
        source=FeedSource.beat("beat-1"),
    )
    await repo.add(post)
    assert await repo.get(post.id) == post


@pytest.mark.asyncio
async def test_list_newest_first(repo: InMemoryFeedPostRepository) -> None:
    base = datetime(2026, 4, 20, 12, 0, tzinfo=timezone.utc)
    old = FeedPost.create(
        character_id="c", kind=FeedKind.MOOD, content_text="old",
        source=FeedSource.beat("b1"), created_at=base,
    )
    new = FeedPost.create(
        character_id="c", kind=FeedKind.MOOD, content_text="new",
        source=FeedSource.beat("b2"),
        created_at=base + timedelta(hours=1),
    )
    await repo.add(old)
    await repo.add(new)
    got = await repo.list_for_character("c")
    assert [p.content_text for p in got] == ["new", "old"]


@pytest.mark.asyncio
async def test_list_with_before_cursor(
    repo: InMemoryFeedPostRepository,
) -> None:
    base = datetime(2026, 4, 20, 12, 0, tzinfo=timezone.utc)
    for idx in range(4):
        await repo.add(FeedPost.create(
            character_id="c",
            kind=FeedKind.MOOD,
            content_text=f"p{idx}",
            source=FeedSource.beat(f"b{idx}"),
            created_at=base + timedelta(minutes=idx),
        ))
    # newest-first → p3, p2, p1, p0. before=base+2min should yield p1, p0.
    page = await repo.list_for_character(
        "c", before=base + timedelta(minutes=2),
    )
    assert [p.content_text for p in page] == ["p1", "p0"]


@pytest.mark.asyncio
async def test_dedup_by_source(repo: InMemoryFeedPostRepository) -> None:
    src = FeedSource.beat("beat-x")
    await repo.add(FeedPost.create(
        character_id="c", kind=FeedKind.SCENE_BEAT,
        content_text="first", source=src,
    ))
    with pytest.raises(ValueError, match="already exists"):
        await repo.add(FeedPost.create(
            character_id="c", kind=FeedKind.SCENE_BEAT,
            content_text="second", source=src,
        ))


@pytest.mark.asyncio
async def test_count_on_date_window(
    repo: InMemoryFeedPostRepository,
) -> None:
    on = datetime(2026, 4, 20, 12, 0, tzinfo=timezone.utc)
    off = datetime(2026, 4, 21, 1, 0, tzinfo=timezone.utc)
    await repo.add(FeedPost.create(
        character_id="c", kind=FeedKind.MOOD,
        content_text="a", source=FeedSource.beat("b1"),
        created_at=on,
    ))
    await repo.add(FeedPost.create(
        character_id="c", kind=FeedKind.MOOD,
        content_text="b", source=FeedSource.beat("b2"),
        created_at=off,
    ))
    assert await repo.count_on_date("c", on=on.date()) == 1


@pytest.mark.asyncio
async def test_count_on_date_uses_explicit_local_day_boundary(
    repo: InMemoryFeedPostRepository,
) -> None:
    local_tz = ZoneInfo("Asia/Taipei")
    local_day_post = datetime(2026, 4, 19, 16, 30, tzinfo=timezone.utc)
    previous_local_day_post = datetime(2026, 4, 19, 15, 59, tzinfo=timezone.utc)
    await repo.add(FeedPost.create(
        character_id="c", kind=FeedKind.MOOD,
        content_text="local day", source=FeedSource.beat("local-day"),
        created_at=local_day_post,
    ))
    await repo.add(FeedPost.create(
        character_id="c", kind=FeedKind.MOOD,
        content_text="previous", source=FeedSource.beat("previous-day"),
        created_at=previous_local_day_post,
    ))

    assert await repo.count_on_date(
        "c", on=datetime(2026, 4, 20).date(), local_tz=local_tz,
    ) == 1


@pytest.mark.asyncio
async def test_latest_for_character(
    repo: InMemoryFeedPostRepository,
) -> None:
    base = datetime(2026, 4, 20, tzinfo=timezone.utc)
    for idx in range(3):
        await repo.add(FeedPost.create(
            character_id="c",
            kind=FeedKind.MOOD,
            content_text=f"x{idx}",
            source=FeedSource.beat(f"b{idx}"),
            created_at=base + timedelta(hours=idx),
        ))
    latest = await repo.latest_for_character("c")
    assert latest is not None
    assert latest.content_text == "x2"


@pytest.mark.asyncio
async def test_find_by_source_matches_kind_and_ref_id(
    repo: InMemoryFeedPostRepository,
) -> None:
    await repo.add(FeedPost.create(
        character_id="c", kind=FeedKind.SCENE_BEAT,
        content_text="…", source=FeedSource.beat("alpha"),
    ))
    found = await repo.find_by_source("c", FeedSource.beat("alpha"))
    assert found is not None
    miss = await repo.find_by_source("c", FeedSource.beat("beta"))
    assert miss is None


@pytest.mark.asyncio
async def test_list_recent_mixes_characters_newest_first(
    repo: InMemoryFeedPostRepository,
) -> None:
    base = datetime(2026, 4, 20, 12, 0, tzinfo=timezone.utc)
    await repo.add(FeedPost.create(
        character_id="a", kind=FeedKind.MOOD,
        content_text="a-old", source=FeedSource.beat("ba1"),
        created_at=base,
    ))
    await repo.add(FeedPost.create(
        character_id="b", kind=FeedKind.MOOD,
        content_text="b-mid", source=FeedSource.beat("bb1"),
        created_at=base + timedelta(minutes=5),
    ))
    await repo.add(FeedPost.create(
        character_id="a", kind=FeedKind.MOOD,
        content_text="a-new", source=FeedSource.beat("ba2"),
        created_at=base + timedelta(minutes=10),
    ))
    page = await repo.list_recent(limit=10)
    assert [p.content_text for p in page] == ["a-new", "b-mid", "a-old"]


@pytest.mark.asyncio
async def test_list_recent_before_cursor_walks_back(
    repo: InMemoryFeedPostRepository,
) -> None:
    base = datetime(2026, 4, 20, 12, 0, tzinfo=timezone.utc)
    for idx in range(4):
        await repo.add(FeedPost.create(
            character_id="a" if idx % 2 == 0 else "b",
            kind=FeedKind.MOOD,
            content_text=f"p{idx}",
            source=FeedSource.beat(f"bx{idx}"),
            created_at=base + timedelta(minutes=idx),
        ))
    page = await repo.list_recent(before=base + timedelta(minutes=2))
    assert [p.content_text for p in page] == ["p1", "p0"]


@pytest.mark.asyncio
async def test_count_since_global_watermark(
    repo: InMemoryFeedPostRepository,
) -> None:
    base = datetime(2026, 4, 20, 12, 0, tzinfo=timezone.utc)
    for idx in range(3):
        await repo.add(FeedPost.create(
            character_id="a" if idx == 0 else "b",
            kind=FeedKind.MOOD,
            content_text=f"p{idx}",
            source=FeedSource.beat(f"bs{idx}"),
            created_at=base + timedelta(minutes=idx),
        ))
    # since=base → strictly > base, so 2 posts qualify (minutes=1 and 2).
    assert await repo.count_since(since=base) == 2
    # since=base+5min → no new posts.
    assert await repo.count_since(since=base + timedelta(minutes=5)) == 0


@pytest.mark.asyncio
async def test_delete_for_character_cascades(
    repo: InMemoryFeedPostRepository,
) -> None:
    for idx in range(3):
        await repo.add(FeedPost.create(
            character_id="c", kind=FeedKind.MOOD,
            content_text=f"x{idx}",
            source=FeedSource.beat(f"b{idx}"),
        ))
    await repo.add(FeedPost.create(
        character_id="other", kind=FeedKind.MOOD,
        content_text="keep", source=FeedSource.beat("ok"),
    ))
    removed = await repo.delete_for_character("c")
    assert removed == 3
    assert await repo.list_for_character("c") == []
    other = await repo.list_for_character("other")
    assert len(other) == 1
