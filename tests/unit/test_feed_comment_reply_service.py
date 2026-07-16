"""Tests for ``FeedCommentReplyService`` (LumeGram Phase B).

Covers the gating logic that decides whether a character should reply
to user comments on a tick, the FIFO candidate picker, the persist +
memorialise path, and the fail-soft contract on composer / embedder
errors.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.application.services.feed_comment_reply_service import (
    FeedCommentReplyService,
)
from kokoro_link.application.services.feed_comment_service import (
    FeedCommentService,
)
from kokoro_link.application.services.feed_event_bus import (
    FeedCommentReplyEvent,
    FeedEventBus,
)
from kokoro_link.contracts.embedder import EmbedderError
from kokoro_link.contracts.feed_comment_reply import (
    FeedCommentReplyComposerPort,
    FeedCommentReplyInput,
    FeedCommentReplyOutput,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.operator_profile import OperatorProfile
from kokoro_link.domain.entities.feed_comment import (
    LOCAL_COMMENTER_ID,
    FeedComment,
)
from kokoro_link.domain.entities.feed_post import FeedPost
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.feed_kind import FeedKind
from kokoro_link.domain.value_objects.feed_source import FeedSource
from kokoro_link.infrastructure.memory.in_memory import InMemoryMemoryRepository
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_feed_comments import (
    InMemoryFeedCommentRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_feed_posts import (
    InMemoryFeedPostRepository,
)


UTC = timezone.utc


class _OperatorProfileService:
    async def get_for_user(self, user_id: str) -> OperatorProfile:
        return OperatorProfile(
            id=user_id,
            display_name=user_id,
            timezone_id="Asia/Taipei",
        )


# ---------- helpers ----------


def _make_character(
    *, id_: str = "aiko", feed_daily_limit: int = 3,
) -> Character:
    char = Character.create(
        name="Aiko",
        summary="",
        personality=[],
        interests=[],
        speaking_style="",
        boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=20, trust=50, energy=80,
        ),
        feed_daily_limit=feed_daily_limit,
    )
    return replace(char, id=id_)


def _make_post(
    *,
    character_id: str = "aiko",
    body: str = "今天的咖啡好喝",
    source: FeedSource | None = None,
    created_at: datetime | None = None,
) -> FeedPost:
    return FeedPost.create(
        character_id=character_id,
        kind=FeedKind.MOOD,
        content_text=body,
        source=source or FeedSource.silence(),
        created_at=created_at or datetime(2026, 4, 28, 9, 0, tzinfo=UTC),
    )


class _ScriptedReplyComposer(FeedCommentReplyComposerPort):
    """Composer that returns canned outputs and records inputs."""

    def __init__(self, outputs: list[FeedCommentReplyOutput | Exception]) -> None:
        self._outputs = list(outputs)
        self.inputs: list[FeedCommentReplyInput] = []

    async def compose(
        self, payload: FeedCommentReplyInput,
    ) -> FeedCommentReplyOutput:
        self.inputs.append(payload)
        if not self._outputs:
            return FeedCommentReplyOutput(content_text="")
        out = self._outputs.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


class _BoomEmbedder:
    is_operational = True

    async def embed_one(self, text: str) -> list[float]:
        raise EmbedderError("offline")

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        raise EmbedderError("offline")


def _build(
    *,
    composer: _ScriptedReplyComposer,
    posts: InMemoryFeedPostRepository,
    comments: InMemoryFeedCommentRepository,
    memories: InMemoryMemoryRepository | None = None,
    embedder=None,
    schedule_service=None,
    daily_cap: int = 6,
    character_repository=None,
    event_bus=None,
    operator_profile_service=None,
) -> FeedCommentReplyService:
    comment_service = FeedCommentService(
        post_repository=posts, comment_repository=comments,
    )
    return FeedCommentReplyService(
        post_repository=posts,
        comment_repository=comments,
        comment_service=comment_service,
        composer=composer,
        memory_repository=memories,
        embedder=embedder,
        schedule_service=schedule_service,
        daily_cap=daily_cap,
        character_repository=character_repository,
        event_bus=event_bus,
        operator_profile_service=operator_profile_service,
    )


# ---------- tests ----------


@pytest.mark.asyncio
async def test_skips_when_feed_disabled() -> None:
    char = _make_character(feed_daily_limit=0)
    posts = InMemoryFeedPostRepository()
    comments = InMemoryFeedCommentRepository()
    composer = _ScriptedReplyComposer([])
    service = _build(composer=composer, posts=posts, comments=comments)

    result = await service.tick(
        char, now=datetime(2026, 4, 28, 12, 0, tzinfo=UTC),
    )
    assert result is None
    assert composer.inputs == []


@pytest.mark.asyncio
async def test_skips_when_no_posts() -> None:
    char = _make_character()
    posts = InMemoryFeedPostRepository()
    comments = InMemoryFeedCommentRepository()
    composer = _ScriptedReplyComposer([])
    service = _build(composer=composer, posts=posts, comments=comments)
    assert await service.tick(
        char, now=datetime(2026, 4, 28, 12, 0, tzinfo=UTC),
    ) is None


@pytest.mark.asyncio
async def test_skips_when_no_user_comments() -> None:
    char = _make_character()
    posts = InMemoryFeedPostRepository()
    comments = InMemoryFeedCommentRepository()
    post = _make_post()
    await posts.add(post)
    composer = _ScriptedReplyComposer([])
    service = _build(composer=composer, posts=posts, comments=comments)
    assert await service.tick(
        char, now=datetime(2026, 4, 28, 12, 0, tzinfo=UTC),
    ) is None


@pytest.mark.asyncio
async def test_skips_when_user_comment_too_new() -> None:
    char = _make_character()
    posts = InMemoryFeedPostRepository()
    comments = InMemoryFeedCommentRepository()
    post = _make_post()
    await posts.add(post)
    now = datetime(2026, 4, 28, 12, 0, tzinfo=UTC)
    await comments.add(FeedComment.create(
        post_id=post.id,
        author_id=LOCAL_COMMENTER_ID,
        content_text="嗨",
        created_at=now - timedelta(seconds=30),  # 30s < 2min min_wait
    ))
    composer = _ScriptedReplyComposer([
        FeedCommentReplyOutput(content_text="嗨～"),
    ])
    service = _build(composer=composer, posts=posts, comments=comments)
    assert await service.tick(char, now=now) is None
    assert composer.inputs == []


@pytest.mark.asyncio
async def test_skips_when_user_comment_too_old() -> None:
    char = _make_character()
    posts = InMemoryFeedPostRepository()
    comments = InMemoryFeedCommentRepository()
    post = _make_post()
    await posts.add(post)
    now = datetime(2026, 4, 30, 12, 0, tzinfo=UTC)
    await comments.add(FeedComment.create(
        post_id=post.id,
        author_id=LOCAL_COMMENTER_ID,
        content_text="嗨",
        created_at=now - timedelta(hours=72),  # > 48h max_age
    ))
    composer = _ScriptedReplyComposer([
        FeedCommentReplyOutput(content_text="嗨～"),
    ])
    service = _build(composer=composer, posts=posts, comments=comments)
    assert await service.tick(char, now=now) is None


@pytest.mark.asyncio
async def test_happy_path_persists_reply_and_memory() -> None:
    char = _make_character()
    posts = InMemoryFeedPostRepository()
    comments = InMemoryFeedCommentRepository()
    memories = InMemoryMemoryRepository()
    post = _make_post()
    await posts.add(post)
    now = datetime(2026, 4, 28, 12, 0, tzinfo=UTC)
    await comments.add(FeedComment.create(
        post_id=post.id,
        author_id=LOCAL_COMMENTER_ID,
        content_text="這杯看起來好好喝",
        created_at=now - timedelta(minutes=10),
    ))
    composer = _ScriptedReplyComposer([
        FeedCommentReplyOutput(content_text="謝啦～改天請你喝 ☕"),
    ])
    service = _build(
        composer=composer, posts=posts, comments=comments, memories=memories,
    )

    reply = await service.tick(char, now=now)
    assert reply is not None
    assert reply.author_id == char.id
    assert reply.content_text == "謝啦～改天請你喝 ☕"
    assert reply.post_id == post.id

    # The post's denormalised comment counter rises to 2 (user + reply).
    refreshed = await posts.get(post.id)
    assert refreshed.reactions.comments == 2

    # Episodic memory written with the reply preview.
    written = await memories.query(char.id, limit=10)
    assert len(written) == 1
    assert "feed_comment_reply" in written[0].tags
    assert "謝啦" in written[0].content


@pytest.mark.asyncio
async def test_skips_when_inside_cooldown() -> None:
    char = _make_character()
    posts = InMemoryFeedPostRepository()
    comments = InMemoryFeedCommentRepository()
    post = _make_post()
    await posts.add(post)
    now = datetime(2026, 4, 28, 12, 0, tzinfo=UTC)
    # Previous character reply 5 minutes ago — inside the 20-min cooldown.
    await comments.add(FeedComment.create(
        post_id=post.id,
        author_id=char.id,
        content_text="先前回過了",
        created_at=now - timedelta(minutes=5),
    ))
    # New user comment 4 minutes ago (after the previous reply, so it
    # IS unanswered relative to the watermark).
    await comments.add(FeedComment.create(
        post_id=post.id,
        author_id=LOCAL_COMMENTER_ID,
        content_text="再聊聊？",
        created_at=now - timedelta(minutes=4),
    ))
    composer = _ScriptedReplyComposer([
        FeedCommentReplyOutput(content_text="嗨"),
    ])
    service = _build(composer=composer, posts=posts, comments=comments)
    assert await service.tick(char, now=now) is None
    assert composer.inputs == []


@pytest.mark.asyncio
async def test_skips_when_daily_cap_reached() -> None:
    char = _make_character()
    posts = InMemoryFeedPostRepository()
    comments = InMemoryFeedCommentRepository()
    post = _make_post()
    await posts.add(post)
    now = datetime(2026, 4, 28, 22, 0, tzinfo=UTC)
    # Two prior character replies same UTC day; daily_cap=2 in this test.
    for offset in (5, 4):
        await comments.add(FeedComment.create(
            post_id=post.id,
            author_id=char.id,
            content_text=f"先前回過了 {offset}",
            created_at=datetime(2026, 4, 28, offset, 0, tzinfo=UTC),
        ))
    # Fresh user comment 1 hour ago (well past min_wait, past cooldown).
    await comments.add(FeedComment.create(
        post_id=post.id,
        author_id=LOCAL_COMMENTER_ID,
        content_text="嗨～",
        created_at=now - timedelta(hours=1),
    ))
    composer = _ScriptedReplyComposer([
        FeedCommentReplyOutput(content_text="不該回"),
    ])
    service = _build(
        composer=composer, posts=posts, comments=comments, daily_cap=2,
    )
    assert await service.tick(char, now=now) is None


@pytest.mark.asyncio
async def test_daily_cap_uses_owner_timezone_not_utc_day() -> None:
    char = replace(_make_character(), user_id="owner-tw")
    posts = InMemoryFeedPostRepository()
    comments = InMemoryFeedCommentRepository()
    post = _make_post()
    await posts.add(post)
    now = datetime(2026, 6, 15, 2, 0, tzinfo=UTC)
    # These are UTC 2026-06-14 but Asia/Taipei 2026-06-15.
    for minute in (0, 4):
        await comments.add(FeedComment.create(
            post_id=post.id,
            author_id=char.id,
            content_text=f"先前回過了 {minute}",
            created_at=datetime(2026, 6, 14, 22, minute, tzinfo=UTC),
        ))
    await comments.add(FeedComment.create(
        post_id=post.id,
        author_id=LOCAL_COMMENTER_ID,
        content_text="現在可以聊了嗎？",
        created_at=now - timedelta(hours=1),
    ))
    composer = _ScriptedReplyComposer([
        FeedCommentReplyOutput(content_text="不該回"),
    ])
    service = _build(
        composer=composer,
        posts=posts,
        comments=comments,
        daily_cap=2,
        operator_profile_service=_OperatorProfileService(),
    )

    assert await service.tick(char, now=now) is None
    assert composer.inputs == []


@pytest.mark.asyncio
async def test_picks_oldest_unanswered_batch_first() -> None:
    char = _make_character()
    posts = InMemoryFeedPostRepository()
    comments = InMemoryFeedCommentRepository()
    now = datetime(2026, 4, 28, 12, 0, tzinfo=UTC)

    older_post = _make_post(
        body="舊文",
        source=FeedSource.memory("m-old"),
        created_at=now - timedelta(days=2),
    )
    newer_post = _make_post(
        body="新文",
        source=FeedSource.memory("m-new"),
        created_at=now - timedelta(hours=6),
    )
    await posts.add(older_post)
    await posts.add(newer_post)

    # Older post: a user comment 30 minutes ago.
    await comments.add(FeedComment.create(
        post_id=older_post.id,
        author_id=LOCAL_COMMENTER_ID,
        content_text="舊留言",
        created_at=now - timedelta(minutes=30),
    ))
    # Newer post: a user comment 10 minutes ago (warmer but younger).
    await comments.add(FeedComment.create(
        post_id=newer_post.id,
        author_id=LOCAL_COMMENTER_ID,
        content_text="新留言",
        created_at=now - timedelta(minutes=10),
    ))

    composer = _ScriptedReplyComposer([
        FeedCommentReplyOutput(content_text="回應"),
    ])
    service = _build(composer=composer, posts=posts, comments=comments)

    reply = await service.tick(char, now=now)
    assert reply is not None
    # FIFO — older batch wins.
    assert reply.post_id == older_post.id
    assert composer.inputs[0].post.id == older_post.id


@pytest.mark.asyncio
async def test_folds_multiple_user_comments_into_one_reply() -> None:
    char = _make_character()
    posts = InMemoryFeedPostRepository()
    comments = InMemoryFeedCommentRepository()
    post = _make_post()
    await posts.add(post)
    now = datetime(2026, 4, 28, 12, 0, tzinfo=UTC)
    for i, text in enumerate(["第一句", "想到第二句", "還有第三句"]):
        await comments.add(FeedComment.create(
            post_id=post.id,
            author_id=LOCAL_COMMENTER_ID,
            content_text=text,
            created_at=now - timedelta(minutes=10 - i),
        ))
    composer = _ScriptedReplyComposer([
        FeedCommentReplyOutput(content_text="一次回三則"),
    ])
    service = _build(composer=composer, posts=posts, comments=comments)

    reply = await service.tick(char, now=now)
    assert reply is not None
    payload = composer.inputs[0]
    assert len(payload.user_comments) == 3
    assert [c.content_text for c in payload.user_comments] == [
        "第一句", "想到第二句", "還有第三句",
    ]


@pytest.mark.asyncio
async def test_skips_when_composer_returns_empty() -> None:
    char = _make_character()
    posts = InMemoryFeedPostRepository()
    comments = InMemoryFeedCommentRepository()
    post = _make_post()
    await posts.add(post)
    now = datetime(2026, 4, 28, 12, 0, tzinfo=UTC)
    await comments.add(FeedComment.create(
        post_id=post.id,
        author_id=LOCAL_COMMENTER_ID,
        content_text="hi",
        created_at=now - timedelta(minutes=10),
    ))
    composer = _ScriptedReplyComposer([
        FeedCommentReplyOutput(content_text=""),
    ])
    service = _build(composer=composer, posts=posts, comments=comments)
    assert await service.tick(char, now=now) is None
    # No character row was inserted.
    rows = await comments.list_for_post(post.id, limit=10)
    assert all(c.author_id == LOCAL_COMMENTER_ID for c in rows)


@pytest.mark.asyncio
async def test_skips_when_high_busy() -> None:
    """When schedule_service reports a current activity with busy_score
    >= 0.85 the service should skip this tick."""
    char = _make_character()
    posts = InMemoryFeedPostRepository()
    comments = InMemoryFeedCommentRepository()
    post = _make_post()
    await posts.add(post)
    now = datetime(2026, 4, 28, 12, 0, tzinfo=UTC)
    await comments.add(FeedComment.create(
        post_id=post.id,
        author_id=LOCAL_COMMENTER_ID,
        content_text="hi",
        created_at=now - timedelta(minutes=10),
    ))

    class _BusySchedule:
        def __init__(self) -> None:
            self.received_character = None

        async def current_activity_response(
            self, character_id, *, now=None, character=None,
        ):
            self.received_character = character
            class _Resp:
                class current:
                    busy_score = 0.95
            return _Resp()

    composer = _ScriptedReplyComposer([
        FeedCommentReplyOutput(content_text="不該回"),
    ])
    busy_schedule = _BusySchedule()
    service = _build(
        composer=composer, posts=posts, comments=comments,
        schedule_service=busy_schedule,
    )
    assert await service.tick(char, now=now) is None
    assert composer.inputs == []
    assert busy_schedule.received_character is char


@pytest.mark.asyncio
async def test_increments_unread_counter_and_publishes_event() -> None:
    """When character_repository + event_bus are wired, a successful
    reply bumps the unread badge and broadcasts a SSE event carrying
    the post-increment count."""
    char = _make_character()
    char_repo = InMemoryCharacterRepository()
    await char_repo.save(char)

    posts = InMemoryFeedPostRepository()
    comments = InMemoryFeedCommentRepository()
    post = _make_post()
    await posts.add(post)
    now = datetime(2026, 4, 28, 12, 0, tzinfo=UTC)
    await comments.add(FeedComment.create(
        post_id=post.id,
        author_id=LOCAL_COMMENTER_ID,
        content_text="嗨",
        created_at=now - timedelta(minutes=10),
    ))
    composer = _ScriptedReplyComposer([
        FeedCommentReplyOutput(content_text="嗨～"),
    ])

    bus = FeedEventBus()
    captured: list[FeedCommentReplyEvent] = []

    async def _drain():
        async with bus.subscription() as queue:
            evt = await queue.get()
            captured.append(evt)

    import asyncio
    drain_task = asyncio.create_task(_drain())
    # Give the subscriber a chance to register before publishing.
    await asyncio.sleep(0)

    service = _build(
        composer=composer, posts=posts, comments=comments,
        character_repository=char_repo, event_bus=bus,
    )
    reply = await service.tick(char, now=now)
    assert reply is not None

    await asyncio.wait_for(drain_task, timeout=1.0)

    refreshed = await char_repo.get(char.id)
    assert refreshed.unread_feed_reply_count == 1

    assert len(captured) == 1
    assert captured[0].character_id == char.id
    assert captured[0].comment_id == reply.id
    assert captured[0].unread_count == 1


@pytest.mark.asyncio
async def test_unread_counter_accumulates_on_top_of_existing() -> None:
    """A new reply increments whatever count the character already has,
    not an absolute reset to 1."""
    char = _make_character()
    # Pretend two prior replies already landed (e.g. across past days)
    # so we can exercise the increment-from-existing path without
    # racing FeedComment's wall-clock created_at.
    char = char.with_unread_feed_reply(2)
    char_repo = InMemoryCharacterRepository()
    await char_repo.save(char)

    posts = InMemoryFeedPostRepository()
    comments = InMemoryFeedCommentRepository()
    post = _make_post()
    await posts.add(post)
    now = datetime(2026, 4, 28, 12, 0, tzinfo=UTC)
    await comments.add(FeedComment.create(
        post_id=post.id, author_id=LOCAL_COMMENTER_ID,
        content_text="hi", created_at=now - timedelta(minutes=10),
    ))
    composer = _ScriptedReplyComposer([
        FeedCommentReplyOutput(content_text="嗨"),
    ])
    service = _build(
        composer=composer, posts=posts, comments=comments,
        character_repository=char_repo,
    )
    reply = await service.tick(char, now=now)
    assert reply is not None

    refreshed = await char_repo.get(char.id)
    assert refreshed.unread_feed_reply_count == 3


@pytest.mark.asyncio
async def test_persists_reply_even_when_embedder_fails() -> None:
    """An offline embedder must not block the reply landing — the row
    is the user-visible artefact; memory is best-effort."""
    char = _make_character()
    posts = InMemoryFeedPostRepository()
    comments = InMemoryFeedCommentRepository()
    memories = InMemoryMemoryRepository()
    post = _make_post()
    await posts.add(post)
    now = datetime(2026, 4, 28, 12, 0, tzinfo=UTC)
    await comments.add(FeedComment.create(
        post_id=post.id,
        author_id=LOCAL_COMMENTER_ID,
        content_text="hi",
        created_at=now - timedelta(minutes=10),
    ))
    composer = _ScriptedReplyComposer([
        FeedCommentReplyOutput(content_text="嗨～"),
    ])
    service = _build(
        composer=composer, posts=posts, comments=comments,
        memories=memories, embedder=_BoomEmbedder(),
    )
    reply = await service.tick(char, now=now)
    assert reply is not None
    assert reply.content_text == "嗨～"
    # No memory was written (embedder fail-loud, persist skipped).
    assert await memories.query(char.id, limit=10) == []
