"""Unit tests for ``FeedReactionService``.

Pin the toggle semantics: ``like`` is idempotent, ``unlike`` is
idempotent, both 404 on a missing post, and the denormalised
``FeedPost.reactions.likes`` count stays in sync after every flip.
"""

from __future__ import annotations

import pytest

from kokoro_link.application.services.feed_reaction_service import (
    FeedPostNotFound,
    FeedReactionService,
)
from kokoro_link.domain.entities.feed_post import FeedPost
from kokoro_link.domain.entities.feed_reaction import LOCAL_LIKER_ID
from kokoro_link.domain.value_objects.feed_kind import FeedKind
from kokoro_link.domain.value_objects.feed_source import FeedSource
from kokoro_link.infrastructure.repositories.in_memory_feed_posts import (
    InMemoryFeedPostRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_feed_reactions import (
    InMemoryFeedReactionRepository,
)


def _make_service() -> tuple[
    FeedReactionService,
    InMemoryFeedPostRepository,
    InMemoryFeedReactionRepository,
]:
    posts = InMemoryFeedPostRepository()
    reactions = InMemoryFeedReactionRepository()
    service = FeedReactionService(
        post_repository=posts, reaction_repository=reactions,
    )
    return service, posts, reactions


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
async def test_like_creates_reaction_and_bumps_count() -> None:
    service, posts, reactions = _make_service()
    post = await _seed_post(posts)

    state = await service.like(post_id=post.id)

    assert state.post_id == post.id
    assert state.liked is True
    assert state.likes == 1
    stored = await posts.get(post.id)
    assert stored is not None
    assert stored.reactions.likes == 1
    assert await reactions.has_liked(
        post_id=post.id, liker_id=LOCAL_LIKER_ID,
    )


@pytest.mark.asyncio
async def test_like_is_idempotent() -> None:
    service, posts, _ = _make_service()
    post = await _seed_post(posts)

    first = await service.like(post_id=post.id)
    second = await service.like(post_id=post.id)

    assert first.likes == 1
    assert second.likes == 1
    stored = await posts.get(post.id)
    assert stored is not None
    assert stored.reactions.likes == 1


@pytest.mark.asyncio
async def test_unlike_removes_reaction_and_clears_count() -> None:
    service, posts, reactions = _make_service()
    post = await _seed_post(posts)
    await service.like(post_id=post.id)

    state = await service.unlike(post_id=post.id)

    assert state.liked is False
    assert state.likes == 0
    stored = await posts.get(post.id)
    assert stored is not None
    assert stored.reactions.likes == 0
    assert not await reactions.has_liked(
        post_id=post.id, liker_id=LOCAL_LIKER_ID,
    )


@pytest.mark.asyncio
async def test_unlike_is_idempotent_on_unliked_post() -> None:
    service, posts, _ = _make_service()
    post = await _seed_post(posts)

    state = await service.unlike(post_id=post.id)

    assert state.liked is False
    assert state.likes == 0


@pytest.mark.asyncio
async def test_like_unknown_post_raises() -> None:
    service, _, _ = _make_service()

    with pytest.raises(FeedPostNotFound):
        await service.like(post_id="ghost")


@pytest.mark.asyncio
async def test_unlike_unknown_post_raises() -> None:
    service, _, _ = _make_service()

    with pytest.raises(FeedPostNotFound):
        await service.unlike(post_id="ghost")


@pytest.mark.asyncio
async def test_state_for_reflects_current_like_status() -> None:
    service, posts, _ = _make_service()
    post = await _seed_post(posts)

    before = await service.state_for(post_id=post.id)
    assert before.liked is False
    assert before.likes == 0

    await service.like(post_id=post.id)
    after = await service.state_for(post_id=post.id)
    assert after.liked is True
    assert after.likes == 1


@pytest.mark.asyncio
async def test_distinct_likers_accumulate_count() -> None:
    """Forward-compat: multi-user mode should count distinct likers
    independently. Single-user routes today only stamp ``LOCAL_LIKER_ID``,
    but the service / repo must already be liker-aware."""
    service, posts, _ = _make_service()
    post = await _seed_post(posts)

    await service.like(post_id=post.id, liker_id="user-a")
    state = await service.like(post_id=post.id, liker_id="user-b")

    assert state.likes == 2
    stored = await posts.get(post.id)
    assert stored is not None
    assert stored.reactions.likes == 2
