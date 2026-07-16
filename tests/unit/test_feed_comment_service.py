"""Unit tests for ``FeedCommentService``.

Pin: add validates / persists / bumps the denormalised count, list is
reverse-chronological, removing your own comment is allowed and
recounts, removing somebody else's comment is forbidden.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.application.services.feed_comment_service import (
    FeedCommentForbidden,
    FeedCommentNotFound,
    FeedCommentService,
    FeedPostNotFound,
)
from kokoro_link.domain.entities.feed_comment import LOCAL_COMMENTER_ID
from kokoro_link.domain.entities.feed_post import FeedPost
from kokoro_link.domain.value_objects.feed_kind import FeedKind
from kokoro_link.domain.value_objects.feed_source import FeedSource
from kokoro_link.infrastructure.repositories.in_memory_feed_comments import (
    InMemoryFeedCommentRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_feed_posts import (
    InMemoryFeedPostRepository,
)


def _make_service():
    posts = InMemoryFeedPostRepository()
    comments = InMemoryFeedCommentRepository()
    service = FeedCommentService(
        post_repository=posts, comment_repository=comments,
    )
    return service, posts, comments


async def _seed_post(posts: InMemoryFeedPostRepository) -> FeedPost:
    post = FeedPost.create(
        character_id="char-1",
        kind=FeedKind.MOOD,
        content_text="hello",
        source=FeedSource.beat("b1"),
    )
    await posts.add(post)
    return post


@pytest.mark.asyncio
async def test_add_persists_comment_and_bumps_count() -> None:
    service, posts, comments = _make_service()
    post = await _seed_post(posts)

    stored = await service.add(post_id=post.id, content_text="nice!")

    assert stored.post_id == post.id
    assert stored.content_text == "nice!"
    assert stored.author_id == LOCAL_COMMENTER_ID
    assert await comments.count_for_post(post.id) == 1
    fresh = await posts.get(post.id)
    assert fresh is not None
    assert fresh.reactions.comments == 1


@pytest.mark.asyncio
async def test_add_trims_whitespace_and_rejects_blank() -> None:
    service, posts, _ = _make_service()
    post = await _seed_post(posts)

    stored = await service.add(post_id=post.id, content_text="  hi  ")
    assert stored.content_text == "hi"

    with pytest.raises(ValueError):
        await service.add(post_id=post.id, content_text="   ")


@pytest.mark.asyncio
async def test_add_rejects_unknown_post() -> None:
    service, _, _ = _make_service()

    with pytest.raises(FeedPostNotFound):
        await service.add(post_id="ghost", content_text="hi")


@pytest.mark.asyncio
async def test_list_returns_newest_first() -> None:
    service, posts, comments = _make_service()
    post = await _seed_post(posts)
    base = datetime(2026, 4, 29, 10, 0, tzinfo=timezone.utc)
    from kokoro_link.domain.entities.feed_comment import FeedComment
    for idx in range(3):
        await comments.add(FeedComment.create(
            post_id=post.id,
            content_text=f"c{idx}",
            created_at=base + timedelta(minutes=idx),
        ))

    items = await service.list_for_post(post.id)

    assert [c.content_text for c in items] == ["c2", "c1", "c0"]


@pytest.mark.asyncio
async def test_remove_drops_comment_and_decrements_count() -> None:
    service, posts, _ = _make_service()
    post = await _seed_post(posts)
    a = await service.add(post_id=post.id, content_text="first")
    await service.add(post_id=post.id, content_text="second")
    assert (await posts.get(post.id)).reactions.comments == 2

    await service.remove(comment_id=a.id)

    fresh = await posts.get(post.id)
    assert fresh is not None
    assert fresh.reactions.comments == 1


@pytest.mark.asyncio
async def test_remove_unknown_raises_not_found() -> None:
    service, _, _ = _make_service()

    with pytest.raises(FeedCommentNotFound):
        await service.remove(comment_id="ghost")


@pytest.mark.asyncio
async def test_remove_other_users_comment_is_forbidden() -> None:
    service, posts, _ = _make_service()
    post = await _seed_post(posts)
    foreign = await service.add(
        post_id=post.id, content_text="hi", author_id="user-x",
    )

    with pytest.raises(FeedCommentForbidden):
        await service.remove(
            comment_id=foreign.id, author_id=LOCAL_COMMENTER_ID,
        )
