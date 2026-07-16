"""Tests for ``FeedReactionMemorializer``.

Covers the watermark idempotency, like-only / comment-bearing memory
shapes, fail-soft behavior on embedder outage, and the recent-window
scan limit.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.application.services.feed_reaction_memorializer import (
    FeedReactionMemorializer,
)
from kokoro_link.contracts.embedder import EmbedderError
from kokoro_link.domain.entities.feed_comment import FeedComment
from kokoro_link.domain.entities.feed_post import FeedPost
from kokoro_link.domain.entities.feed_reaction import FeedReaction
from kokoro_link.domain.value_objects.feed_kind import FeedKind
from kokoro_link.domain.value_objects.feed_source import FeedSource
from kokoro_link.domain.value_objects.memory_kind import MemoryKind
from kokoro_link.infrastructure.memory.in_memory import InMemoryMemoryRepository
from kokoro_link.infrastructure.repositories.in_memory_feed_comments import (
    InMemoryFeedCommentRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_feed_posts import (
    InMemoryFeedPostRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_feed_reactions import (
    InMemoryFeedReactionRepository,
)

UTC = timezone.utc


def _make_post(
    *,
    character_id: str = "c1",
    body: str = "今天去咖啡廳寫稿",
    created_at: datetime | None = None,
    reactions_seen_at: datetime | None = None,
) -> FeedPost:
    return FeedPost.create(
        character_id=character_id,
        kind=FeedKind.MOOD,
        content_text=body,
        source=FeedSource.silence(),
        created_at=created_at or datetime(2026, 4, 28, 9, 0, tzinfo=UTC),
        reactions_seen_at=reactions_seen_at,
    )


class _BoomEmbedder:
    is_operational = True

    async def embed_one(self, text: str) -> list[float]:
        raise EmbedderError("offline")

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        raise EmbedderError("offline")


@pytest.mark.asyncio
async def test_memorializes_like_with_low_salience() -> None:
    posts = InMemoryFeedPostRepository()
    reactions = InMemoryFeedReactionRepository()
    comments = InMemoryFeedCommentRepository()
    memories = InMemoryMemoryRepository()
    post = _make_post()
    await posts.add(post)
    await reactions.add(
        FeedReaction.create(
            post_id=post.id,
            created_at=datetime(2026, 4, 28, 10, 0, tzinfo=UTC),
        )
    )

    memorializer = FeedReactionMemorializer(
        post_repository=posts,
        reaction_repository=reactions,
        comment_repository=comments,
        memory_repository=memories,
    )
    updated = await memorializer.memorialize(
        character_id="c1",
        now=datetime(2026, 4, 28, 12, 0, tzinfo=UTC),
    )
    assert updated == 1

    written = await memories.query("c1", limit=10)
    assert len(written) == 1
    assert written[0].kind == MemoryKind.EPISODIC
    assert written[0].salience == pytest.approx(0.40)
    assert "feed_reaction" in written[0].tags
    assert "feed_comment" not in written[0].tags
    assert "按了讚" in written[0].content


@pytest.mark.asyncio
async def test_comment_memory_includes_preview_and_higher_salience() -> None:
    posts = InMemoryFeedPostRepository()
    reactions = InMemoryFeedReactionRepository()
    comments = InMemoryFeedCommentRepository()
    memories = InMemoryMemoryRepository()
    post = _make_post(body="今天試了新的拉花")
    await posts.add(post)
    await comments.add(
        FeedComment.create(
            post_id=post.id,
            content_text="我也想喝！下次帶我去",
            created_at=datetime(2026, 4, 28, 11, 0, tzinfo=UTC),
        )
    )

    memorializer = FeedReactionMemorializer(
        post_repository=posts,
        reaction_repository=reactions,
        comment_repository=comments,
        memory_repository=memories,
    )
    updated = await memorializer.memorialize(character_id="c1")
    assert updated == 1

    written = await memories.query("c1", limit=10)
    assert len(written) == 1
    assert written[0].salience == pytest.approx(0.50)
    assert "feed_comment" in written[0].tags
    assert "我也想喝" in written[0].content


@pytest.mark.asyncio
async def test_watermark_makes_second_run_idempotent() -> None:
    posts = InMemoryFeedPostRepository()
    reactions = InMemoryFeedReactionRepository()
    comments = InMemoryFeedCommentRepository()
    memories = InMemoryMemoryRepository()
    post = _make_post()
    await posts.add(post)
    await reactions.add(
        FeedReaction.create(
            post_id=post.id,
            created_at=datetime(2026, 4, 28, 10, 0, tzinfo=UTC),
        )
    )

    memorializer = FeedReactionMemorializer(
        post_repository=posts,
        reaction_repository=reactions,
        comment_repository=comments,
        memory_repository=memories,
    )
    first = await memorializer.memorialize(
        character_id="c1",
        now=datetime(2026, 4, 28, 12, 0, tzinfo=UTC),
    )
    second = await memorializer.memorialize(
        character_id="c1",
        now=datetime(2026, 4, 28, 13, 0, tzinfo=UTC),
    )
    assert first == 1
    assert second == 0
    written = await memories.query("c1", limit=10)
    assert len(written) == 1


@pytest.mark.asyncio
async def test_only_new_interactions_after_watermark_are_memorialized() -> None:
    posts = InMemoryFeedPostRepository()
    reactions = InMemoryFeedReactionRepository()
    comments = InMemoryFeedCommentRepository()
    memories = InMemoryMemoryRepository()
    seen_at = datetime(2026, 4, 28, 12, 0, tzinfo=UTC)
    post = _make_post(reactions_seen_at=seen_at)
    await posts.add(post)
    # Old like (already counted by watermark) — should be ignored.
    await reactions.add(
        FeedReaction.create(
            post_id=post.id,
            liker_id="local",
            created_at=seen_at - timedelta(hours=2),
        )
    )
    # New comment after watermark — should be memorialized.
    await comments.add(
        FeedComment.create(
            post_id=post.id,
            content_text="加油！",
            created_at=seen_at + timedelta(minutes=30),
        )
    )

    memorializer = FeedReactionMemorializer(
        post_repository=posts,
        reaction_repository=reactions,
        comment_repository=comments,
        memory_repository=memories,
    )
    updated = await memorializer.memorialize(
        character_id="c1",
        now=seen_at + timedelta(hours=1),
    )
    assert updated == 1
    written = await memories.query("c1", limit=10)
    assert len(written) == 1
    assert "加油" in written[0].content
    # The old like must NOT be re-counted.
    assert "按了讚" not in written[0].content


@pytest.mark.asyncio
async def test_embedder_failure_defers_memorialization() -> None:
    posts = InMemoryFeedPostRepository()
    reactions = InMemoryFeedReactionRepository()
    comments = InMemoryFeedCommentRepository()
    memories = InMemoryMemoryRepository()
    post = _make_post()
    await posts.add(post)
    await reactions.add(FeedReaction.create(post_id=post.id))

    memorializer = FeedReactionMemorializer(
        post_repository=posts,
        reaction_repository=reactions,
        comment_repository=comments,
        memory_repository=memories,
        embedder=_BoomEmbedder(),
    )
    updated = await memorializer.memorialize(character_id="c1")
    assert updated == 0
    # Watermark must NOT advance, so a later retry can still pick up.
    refreshed = await posts.get(post.id)
    assert refreshed is not None
    assert refreshed.reactions_seen_at is None
    assert (await memories.query("c1", limit=10)) == []


@pytest.mark.asyncio
async def test_no_interactions_returns_zero() -> None:
    posts = InMemoryFeedPostRepository()
    reactions = InMemoryFeedReactionRepository()
    comments = InMemoryFeedCommentRepository()
    memories = InMemoryMemoryRepository()
    await posts.add(_make_post())

    memorializer = FeedReactionMemorializer(
        post_repository=posts,
        reaction_repository=reactions,
        comment_repository=comments,
        memory_repository=memories,
    )
    updated = await memorializer.memorialize(character_id="c1")
    assert updated == 0
    assert (await memories.query("c1", limit=10)) == []


@pytest.mark.asyncio
async def test_character_self_replies_are_excluded_from_user_memory() -> None:
    """Phase B regression: a tick-driven character reply lands in
    ``feed_comments`` with ``author_id == character.id``. The reaction
    memorializer must not fold it into a "user said …" memory — that
    framing belongs to ``FeedCommentReplyService`` which writes the
    correct first-person memory separately."""
    posts = InMemoryFeedPostRepository()
    reactions = InMemoryFeedReactionRepository()
    comments = InMemoryFeedCommentRepository()
    memories = InMemoryMemoryRepository()
    post = _make_post()
    await posts.add(post)
    # Real user comment.
    await comments.add(FeedComment.create(
        post_id=post.id,
        author_id="local",
        content_text="羽衣最棒了",
        created_at=datetime(2026, 4, 28, 10, 0, tzinfo=UTC),
    ))
    # Character's own scheduler-tick reply (author_id == character.id).
    await comments.add(FeedComment.create(
        post_id=post.id,
        author_id="c1",
        content_text="哼，雜魚大叔",
        created_at=datetime(2026, 4, 28, 10, 5, tzinfo=UTC),
    ))

    memorializer = FeedReactionMemorializer(
        post_repository=posts,
        reaction_repository=reactions,
        comment_repository=comments,
        memory_repository=memories,
    )
    updated = await memorializer.memorialize(
        character_id="c1",
        now=datetime(2026, 4, 28, 11, 0, tzinfo=UTC),
    )
    assert updated == 1

    written = await memories.query("c1", limit=10)
    assert len(written) == 1
    body = written[0].content
    # User's actual line is in the memory.
    assert "羽衣最棒了" in body
    # Character's own reply must NOT appear here — it would invert the
    # speaker attribution and confuse later recall.
    assert "雜魚大叔" not in body


def _has_han(text: str) -> bool:
    return any("一" <= ch <= "鿿" for ch in text)


def _has_kana(text: str) -> bool:
    return any("぀" <= ch <= "ヿ" for ch in text)


class _FakeOperatorProfile:
    def __init__(self, primary_language: str) -> None:
        self.primary_language = primary_language


class _FakeOperatorProfileService:
    def __init__(self, primary_language: str) -> None:
        self._primary_language = primary_language

    async def get_for_user(self, user_id: str):  # noqa: ARG002
        return _FakeOperatorProfile(self._primary_language)


class _FakeCharacterRepository:
    def __init__(self, user_id: str = "op-1") -> None:
        self._user_id = user_id

    async def get(self, character_id: str):
        from types import SimpleNamespace
        return SimpleNamespace(id=character_id, user_id=self._user_id)


@pytest.mark.asyncio
async def test_reaction_memory_localizes_to_english() -> None:
    """``_delta_to_memory`` hardcoded a zh-TW sentence template
    regardless of the owning operator's ``primary_language``."""
    posts = InMemoryFeedPostRepository()
    reactions = InMemoryFeedReactionRepository()
    comments = InMemoryFeedCommentRepository()
    memories = InMemoryMemoryRepository()
    post = _make_post(body="Tried a new latte art today")
    await posts.add(post)
    await comments.add(
        FeedComment.create(
            post_id=post.id,
            content_text="I want to try that too!",
            created_at=datetime(2026, 4, 28, 11, 0, tzinfo=UTC),
        )
    )

    memorializer = FeedReactionMemorializer(
        post_repository=posts,
        reaction_repository=reactions,
        comment_repository=comments,
        memory_repository=memories,
        character_repository=_FakeCharacterRepository(),
        operator_profile_service=_FakeOperatorProfileService("en-US"),
    )
    updated = await memorializer.memorialize(character_id="c1")
    assert updated == 1

    written = await memories.query("c1", limit=10)
    assert len(written) == 1
    assert not _has_han(written[0].content), written[0].content
    assert "want to try that too" in written[0].content


@pytest.mark.asyncio
async def test_reaction_memory_localizes_to_japanese() -> None:
    posts = InMemoryFeedPostRepository()
    reactions = InMemoryFeedReactionRepository()
    comments = InMemoryFeedCommentRepository()
    memories = InMemoryMemoryRepository()
    post = _make_post()
    await posts.add(post)
    await reactions.add(
        FeedReaction.create(
            post_id=post.id,
            created_at=datetime(2026, 4, 28, 10, 0, tzinfo=UTC),
        )
    )

    memorializer = FeedReactionMemorializer(
        post_repository=posts,
        reaction_repository=reactions,
        comment_repository=comments,
        memory_repository=memories,
        character_repository=_FakeCharacterRepository(),
        operator_profile_service=_FakeOperatorProfileService("ja-JP"),
    )
    updated = await memorializer.memorialize(
        character_id="c1",
        now=datetime(2026, 4, 28, 12, 0, tzinfo=UTC),
    )
    assert updated == 1

    written = await memories.query("c1", limit=10)
    assert len(written) == 1
    assert _has_kana(written[0].content), written[0].content


@pytest.mark.asyncio
async def test_reaction_memory_defaults_to_zh_tw_without_profile_service() -> None:
    posts = InMemoryFeedPostRepository()
    reactions = InMemoryFeedReactionRepository()
    comments = InMemoryFeedCommentRepository()
    memories = InMemoryMemoryRepository()
    post = _make_post()
    await posts.add(post)
    await reactions.add(
        FeedReaction.create(
            post_id=post.id,
            created_at=datetime(2026, 4, 28, 10, 0, tzinfo=UTC),
        )
    )

    memorializer = FeedReactionMemorializer(
        post_repository=posts,
        reaction_repository=reactions,
        comment_repository=comments,
        memory_repository=memories,
    )
    updated = await memorializer.memorialize(
        character_id="c1",
        now=datetime(2026, 4, 28, 12, 0, tzinfo=UTC),
    )
    assert updated == 1

    written = await memories.query("c1", limit=10)
    assert len(written) == 1
    assert _has_han(written[0].content)
    assert "按了讚" in written[0].content


@pytest.mark.asyncio
async def test_only_self_replies_yields_no_memory() -> None:
    """When the only new comment is the character's own reply (no
    user activity, no likes), nothing is memorialised — the watermark
    stays put, and there's no spurious "user said …" row."""
    posts = InMemoryFeedPostRepository()
    reactions = InMemoryFeedReactionRepository()
    comments = InMemoryFeedCommentRepository()
    memories = InMemoryMemoryRepository()
    post = _make_post()
    await posts.add(post)
    await comments.add(FeedComment.create(
        post_id=post.id,
        author_id="c1",
        content_text="自言自語",
        created_at=datetime(2026, 4, 28, 10, 0, tzinfo=UTC),
    ))

    memorializer = FeedReactionMemorializer(
        post_repository=posts,
        reaction_repository=reactions,
        comment_repository=comments,
        memory_repository=memories,
    )
    updated = await memorializer.memorialize(
        character_id="c1",
        now=datetime(2026, 4, 28, 11, 0, tzinfo=UTC),
    )
    assert updated == 0
    assert (await memories.query("c1", limit=10)) == []
