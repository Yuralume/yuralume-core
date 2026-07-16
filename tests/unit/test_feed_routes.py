"""Route tests for ``/api/v1/characters/{id}/feed`` + post lookup.

Phase 1 surface is read-only — the routes only touch the character
service (existence check) and the feed-post repository. We mount the
real router on a minimal ``ServiceContainer`` so the test exercises
dependency wiring + Pydantic serialisation, not just the handler body.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kokoro_link.api.routes.feed import router as feed_router
from kokoro_link.application.services.feed_candidates import FeedCandidate
from kokoro_link.application.services.feed_comment_service import (
    FeedCommentService,
)
from kokoro_link.application.services.feed_composer_service import (
    FeedComposerService,
)
from kokoro_link.application.services.feed_reaction_service import (
    FeedReactionService,
)
from kokoro_link.contracts.feed import (
    FeedComposerInput,
    FeedComposerOutput,
    FeedComposerPort,
)
from kokoro_link.domain.entities.feed_post import FeedPost
from kokoro_link.domain.value_objects.feed_kind import FeedKind
from kokoro_link.domain.value_objects.feed_source import FeedSource
from kokoro_link.infrastructure.repositories.in_memory_feed_comments import (
    InMemoryFeedCommentRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_feed_posts import (
    InMemoryFeedPostRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_feed_reactions import (
    InMemoryFeedReactionRepository,
)


def _wire_reactions(container, posts):
    """Mount a fresh in-memory reaction stack on top of an existing
    feed_post_repository so the like/unlike routes can run end-to-end."""
    reactions = InMemoryFeedReactionRepository()
    container.feed_reaction_repository = reactions
    container.feed_reaction_service = FeedReactionService(
        post_repository=posts, reaction_repository=reactions,
    )
    return reactions


def _wire_comments(container, posts):
    """Mount a fresh in-memory comment stack on top of an existing
    feed_post_repository so the comment routes can run end-to-end."""
    comments = InMemoryFeedCommentRepository()
    container.feed_comment_repository = comments
    container.feed_comment_service = FeedCommentService(
        post_repository=posts, comment_repository=comments,
    )
    return comments


class _NoopCollector:
    async def collect(self, character, *, now) -> tuple[FeedCandidate, ...]:
        return ()


class _NoopComposer(FeedComposerPort):
    async def compose(self, payload: FeedComposerInput) -> FeedComposerOutput:
        return FeedComposerOutput(content_text="", image_prompt="")


def _wire_composer(container, posts):
    """Mount a real ``FeedComposerService`` against ``posts`` so manual
    POSTs flow through the production code path. Collector + composer
    are no-ops because the manual entry point doesn't call them."""
    container.feed_composer_service = FeedComposerService(
        repository=posts,
        candidates=_NoopCollector(),
        composer=_NoopComposer(),
    )
    return container.feed_composer_service
from tests.unit._messaging_harness import (
    build_messaging_harness,
    build_service_container,
    create_character,
)


def _client(container) -> TestClient:
    app = FastAPI()
    app.state.container = container
    app.include_router(feed_router, prefix="/api/v1")
    return TestClient(app)


@pytest.mark.asyncio
async def test_list_feed_returns_404_for_unknown_character() -> None:
    harness = build_messaging_harness()
    container = build_service_container(harness)
    container.feed_post_repository = InMemoryFeedPostRepository()

    response = _client(container).get("/api/v1/characters/ghost/feed")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_list_feed_empty_when_repository_unset() -> None:
    """Test harnesses that skip wiring the feed stack still get a 200
    with an empty page — the frontend's polling code shouldn't break
    just because feed wasn't enabled."""
    harness = build_messaging_harness()
    container = build_service_container(harness)
    character = await create_character(harness)
    # feed_post_repository intentionally left as None.

    response = _client(container).get(
        f"/api/v1/characters/{character.id}/feed",
    )
    assert response.status_code == 200
    body = response.json()
    assert body == {"items": [], "has_more": False, "next_before": None}


@pytest.mark.asyncio
async def test_list_feed_returns_posts_newest_first() -> None:
    harness = build_messaging_harness()
    container = build_service_container(harness)
    repo = InMemoryFeedPostRepository()
    container.feed_post_repository = repo
    character = await create_character(harness)

    base = datetime(2026, 4, 29, 10, 0, tzinfo=timezone.utc)
    for idx in range(3):
        await repo.add(FeedPost.create(
            character_id=character.id,
            kind=FeedKind.MOOD,
            content_text=f"post {idx}",
            source=FeedSource.beat(f"b{idx}"),
            created_at=base + timedelta(minutes=idx),
        ))

    response = _client(container).get(
        f"/api/v1/characters/{character.id}/feed?limit=20",
    )
    assert response.status_code == 200
    body = response.json()
    assert [item["content_text"] for item in body["items"]] == [
        "post 2", "post 1", "post 0",
    ]
    assert body["has_more"] is False
    assert body["next_before"] is None


@pytest.mark.asyncio
async def test_list_feed_signals_more_pages_via_next_before() -> None:
    harness = build_messaging_harness()
    container = build_service_container(harness)
    repo = InMemoryFeedPostRepository()
    container.feed_post_repository = repo
    character = await create_character(harness)

    base = datetime(2026, 4, 29, 10, 0, tzinfo=timezone.utc)
    for idx in range(3):
        await repo.add(FeedPost.create(
            character_id=character.id,
            kind=FeedKind.MOOD,
            content_text=f"p{idx}",
            source=FeedSource.beat(f"b{idx}"),
            created_at=base + timedelta(minutes=idx),
        ))

    response = _client(container).get(
        f"/api/v1/characters/{character.id}/feed?limit=2",
    )
    body = response.json()
    assert [item["content_text"] for item in body["items"]] == ["p2", "p1"]
    assert body["has_more"] is True
    # next_before is the oldest item's created_at (p1's timestamp)
    assert body["next_before"] is not None
    assert body["next_before"].startswith("2026-04-29T10:01")


@pytest.mark.asyncio
async def test_list_feed_walks_back_with_before_cursor() -> None:
    harness = build_messaging_harness()
    container = build_service_container(harness)
    repo = InMemoryFeedPostRepository()
    container.feed_post_repository = repo
    character = await create_character(harness)

    base = datetime(2026, 4, 29, 10, 0, tzinfo=timezone.utc)
    for idx in range(4):
        await repo.add(FeedPost.create(
            character_id=character.id,
            kind=FeedKind.MOOD,
            content_text=f"p{idx}",
            source=FeedSource.beat(f"b{idx}"),
            created_at=base + timedelta(minutes=idx),
        ))

    cursor = (base + timedelta(minutes=2)).isoformat()
    response = _client(container).get(
        f"/api/v1/characters/{character.id}/feed",
        params={"limit": 10, "before": cursor},
    )
    body = response.json()
    # Posts strictly older than cursor (p1 @ +1, p0 @ +0)
    assert [item["content_text"] for item in body["items"]] == ["p1", "p0"]


@pytest.mark.asyncio
async def test_get_feed_post_returns_404_when_repo_missing() -> None:
    harness = build_messaging_harness()
    container = build_service_container(harness)
    # No feed_post_repository

    response = _client(container).get("/api/v1/feed/posts/anything")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_get_feed_post_returns_404_for_unknown_id() -> None:
    harness = build_messaging_harness()
    container = build_service_container(harness)
    container.feed_post_repository = InMemoryFeedPostRepository()

    response = _client(container).get("/api/v1/feed/posts/missing")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_get_feed_post_returns_payload_when_found() -> None:
    harness = build_messaging_harness()
    container = build_service_container(harness)
    repo = InMemoryFeedPostRepository()
    container.feed_post_repository = repo
    character = await create_character(harness)

    post = FeedPost.create(
        character_id=character.id,
        kind=FeedKind.SCENE_BEAT,
        content_text="今天很特別",
        source=FeedSource.beat("beat-1"),
        image_url="/uploads/feed/x/img.png",
    )
    await repo.add(post)

    response = _client(container).get(f"/api/v1/feed/posts/{post.id}")
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == post.id
    assert body["content_text"] == "今天很特別"
    assert body["kind"] == FeedKind.SCENE_BEAT.value
    assert body["source"] == {"kind": "beat", "ref_id": "beat-1"}
    assert body["image_url"] == "/uploads/feed/x/img.png"
    assert body["reactions"] == {"likes": 0, "comments": 0}


@pytest.mark.asyncio
async def test_like_post_returns_state_and_persists() -> None:
    harness = build_messaging_harness()
    container = build_service_container(harness)
    posts = InMemoryFeedPostRepository()
    container.feed_post_repository = posts
    _wire_reactions(container, posts)
    character = await create_character(harness)

    post = FeedPost.create(
        character_id=character.id,
        kind=FeedKind.MOOD,
        content_text="hi",
        source=FeedSource.beat("b1"),
    )
    await posts.add(post)

    response = _client(container).post(f"/api/v1/feed/posts/{post.id}/like")
    assert response.status_code == 200
    body = response.json()
    assert body == {"post_id": post.id, "liked": True, "likes": 1}

    detail = _client(container).get(f"/api/v1/feed/posts/{post.id}").json()
    assert detail["liked"] is True
    assert detail["reactions"]["likes"] == 1


@pytest.mark.asyncio
async def test_like_is_idempotent_via_route() -> None:
    harness = build_messaging_harness()
    container = build_service_container(harness)
    posts = InMemoryFeedPostRepository()
    container.feed_post_repository = posts
    _wire_reactions(container, posts)
    character = await create_character(harness)

    post = FeedPost.create(
        character_id=character.id,
        kind=FeedKind.MOOD,
        content_text="hi",
        source=FeedSource.beat("b2"),
    )
    await posts.add(post)

    client = _client(container)
    first = client.post(f"/api/v1/feed/posts/{post.id}/like").json()
    second = client.post(f"/api/v1/feed/posts/{post.id}/like").json()
    assert first["likes"] == 1
    assert second["likes"] == 1


@pytest.mark.asyncio
async def test_unlike_clears_state_and_count() -> None:
    harness = build_messaging_harness()
    container = build_service_container(harness)
    posts = InMemoryFeedPostRepository()
    container.feed_post_repository = posts
    _wire_reactions(container, posts)
    character = await create_character(harness)

    post = FeedPost.create(
        character_id=character.id,
        kind=FeedKind.MOOD,
        content_text="hi",
        source=FeedSource.beat("b3"),
    )
    await posts.add(post)

    client = _client(container)
    client.post(f"/api/v1/feed/posts/{post.id}/like")

    response = client.delete(f"/api/v1/feed/posts/{post.id}/like")
    assert response.status_code == 200
    body = response.json()
    assert body == {"post_id": post.id, "liked": False, "likes": 0}

    detail = client.get(f"/api/v1/feed/posts/{post.id}").json()
    assert detail["liked"] is False
    assert detail["reactions"]["likes"] == 0


@pytest.mark.asyncio
async def test_like_returns_404_for_unknown_post() -> None:
    harness = build_messaging_harness()
    container = build_service_container(harness)
    posts = InMemoryFeedPostRepository()
    container.feed_post_repository = posts
    _wire_reactions(container, posts)

    response = _client(container).post("/api/v1/feed/posts/ghost/like")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_like_returns_404_when_service_unwired() -> None:
    """If the deployment skipped the reaction stack, like routes should
    surface a clean 404 instead of crashing on a None service."""
    harness = build_messaging_harness()
    container = build_service_container(harness)
    container.feed_post_repository = InMemoryFeedPostRepository()
    # Intentionally NOT wiring the reaction service.

    response = _client(container).post("/api/v1/feed/posts/anything/like")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_list_feed_hydrates_liked_flag() -> None:
    harness = build_messaging_harness()
    container = build_service_container(harness)
    posts = InMemoryFeedPostRepository()
    container.feed_post_repository = posts
    _wire_reactions(container, posts)
    character = await create_character(harness)

    base = datetime(2026, 4, 29, 10, 0, tzinfo=timezone.utc)
    liked_post = FeedPost.create(
        character_id=character.id,
        kind=FeedKind.MOOD,
        content_text="liked",
        source=FeedSource.beat("ba"),
        created_at=base + timedelta(minutes=1),
    )
    other_post = FeedPost.create(
        character_id=character.id,
        kind=FeedKind.MOOD,
        content_text="other",
        source=FeedSource.beat("bb"),
        created_at=base,
    )
    await posts.add(liked_post)
    await posts.add(other_post)

    client = _client(container)
    client.post(f"/api/v1/feed/posts/{liked_post.id}/like")

    body = client.get(f"/api/v1/characters/{character.id}/feed").json()
    items = {item["id"]: item for item in body["items"]}
    assert items[liked_post.id]["liked"] is True
    assert items[other_post.id]["liked"] is False


@pytest.mark.asyncio
async def test_create_comment_persists_and_bumps_count() -> None:
    harness = build_messaging_harness()
    container = build_service_container(harness)
    posts = InMemoryFeedPostRepository()
    container.feed_post_repository = posts
    _wire_comments(container, posts)
    character = await create_character(harness)

    post = FeedPost.create(
        character_id=character.id,
        kind=FeedKind.MOOD,
        content_text="hi",
        source=FeedSource.beat("c1"),
    )
    await posts.add(post)

    response = _client(container).post(
        f"/api/v1/feed/posts/{post.id}/comments",
        json={"content_text": "love this"},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["post_id"] == post.id
    assert body["content_text"] == "love this"
    # Multi-user auth: author_id now comes from the auth dependency
    # (= default singleton user when KOKORO_AUTH_ENABLED is off).
    assert body["author_id"] == "default"
    assert body["author_display_name"] == "操作者"

    detail = _client(container).get(f"/api/v1/feed/posts/{post.id}").json()
    assert detail["reactions"]["comments"] == 1


@pytest.mark.asyncio
async def test_create_comment_rejects_blank_body() -> None:
    harness = build_messaging_harness()
    container = build_service_container(harness)
    posts = InMemoryFeedPostRepository()
    container.feed_post_repository = posts
    _wire_comments(container, posts)
    character = await create_character(harness)

    post = FeedPost.create(
        character_id=character.id,
        kind=FeedKind.MOOD,
        content_text="hi",
        source=FeedSource.beat("c2"),
    )
    await posts.add(post)

    response = _client(container).post(
        f"/api/v1/feed/posts/{post.id}/comments",
        json={"content_text": "   "},
    )
    # Pydantic min_length=1 trims as a literal length check, but a body
    # of just whitespace still has length>=1 — so validation passes the
    # request and the service raises ValueError at trim time, surfaced
    # as a 422.
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_create_comment_returns_404_for_unknown_post() -> None:
    harness = build_messaging_harness()
    container = build_service_container(harness)
    posts = InMemoryFeedPostRepository()
    container.feed_post_repository = posts
    _wire_comments(container, posts)

    response = _client(container).post(
        "/api/v1/feed/posts/ghost/comments",
        json={"content_text": "hi"},
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_list_comments_returns_newest_first() -> None:
    harness = build_messaging_harness()
    container = build_service_container(harness)
    posts = InMemoryFeedPostRepository()
    container.feed_post_repository = posts
    _wire_comments(container, posts)
    character = await create_character(harness)

    post = FeedPost.create(
        character_id=character.id,
        kind=FeedKind.MOOD,
        content_text="hi",
        source=FeedSource.beat("c3"),
    )
    await posts.add(post)

    client = _client(container)
    for body in ("first", "second", "third"):
        client.post(
            f"/api/v1/feed/posts/{post.id}/comments",
            json={"content_text": body},
        )

    response = client.get(f"/api/v1/feed/posts/{post.id}/comments")
    assert response.status_code == 200
    items = response.json()["items"]
    assert [c["content_text"] for c in items] == ["third", "second", "first"]
    assert {c["author_display_name"] for c in items} == {"操作者"}


@pytest.mark.asyncio
async def test_list_comments_returns_empty_when_unwired() -> None:
    harness = build_messaging_harness()
    container = build_service_container(harness)
    container.feed_post_repository = InMemoryFeedPostRepository()
    # Intentionally NOT wiring the comment service.

    response = _client(container).get("/api/v1/feed/posts/anything/comments")
    assert response.status_code == 200
    assert response.json() == {"items": []}


@pytest.mark.asyncio
async def test_delete_comment_removes_and_recounts() -> None:
    harness = build_messaging_harness()
    container = build_service_container(harness)
    posts = InMemoryFeedPostRepository()
    container.feed_post_repository = posts
    _wire_comments(container, posts)
    character = await create_character(harness)

    post = FeedPost.create(
        character_id=character.id,
        kind=FeedKind.MOOD,
        content_text="hi",
        source=FeedSource.beat("c4"),
    )
    await posts.add(post)

    client = _client(container)
    created = client.post(
        f"/api/v1/feed/posts/{post.id}/comments",
        json={"content_text": "tmp"},
    ).json()

    response = client.delete(f"/api/v1/feed/comments/{created['id']}")
    assert response.status_code == 204

    detail = client.get(f"/api/v1/feed/posts/{post.id}").json()
    assert detail["reactions"]["comments"] == 0


@pytest.mark.asyncio
async def test_delete_comment_returns_404_for_unknown_id() -> None:
    harness = build_messaging_harness()
    container = build_service_container(harness)
    container.feed_post_repository = InMemoryFeedPostRepository()
    _wire_comments(container, container.feed_post_repository)

    response = _client(container).delete("/api/v1/feed/comments/ghost")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_list_feed_validates_limit_bounds() -> None:
    harness = build_messaging_harness()
    container = build_service_container(harness)
    container.feed_post_repository = InMemoryFeedPostRepository()
    character = await create_character(harness)

    too_high = _client(container).get(
        f"/api/v1/characters/{character.id}/feed?limit=999",
    )
    assert too_high.status_code == 422
    too_low = _client(container).get(
        f"/api/v1/characters/{character.id}/feed?limit=0",
    )
    assert too_low.status_code == 422


@pytest.mark.asyncio
async def test_create_manual_feed_post_persists_and_returns_201() -> None:
    harness = build_messaging_harness()
    container = build_service_container(harness)
    posts = InMemoryFeedPostRepository()
    container.feed_post_repository = posts
    _wire_composer(container, posts)
    character = await create_character(harness)

    response = _client(container).post(
        f"/api/v1/characters/{character.id}/feed",
        json={"content_text": "今天我去了陽明山"},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["content_text"] == "今天我去了陽明山"
    assert body["source"]["kind"] == "manual"
    assert body["source"]["ref_id"] is None
    assert body["liked"] is False
    saved = await posts.list_for_character(character.id, limit=10)
    assert len(saved) == 1


@pytest.mark.asyncio
async def test_create_manual_feed_post_404_for_unknown_character() -> None:
    harness = build_messaging_harness()
    container = build_service_container(harness)
    posts = InMemoryFeedPostRepository()
    container.feed_post_repository = posts
    _wire_composer(container, posts)

    response = _client(container).post(
        "/api/v1/characters/ghost/feed",
        json={"content_text": "hi"},
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_create_manual_feed_post_503_when_composer_missing() -> None:
    harness = build_messaging_harness()
    container = build_service_container(harness)
    container.feed_post_repository = InMemoryFeedPostRepository()
    # composer intentionally NOT wired
    character = await create_character(harness)

    response = _client(container).post(
        f"/api/v1/characters/{character.id}/feed",
        json={"content_text": "hi"},
    )
    assert response.status_code == 503


@pytest.mark.asyncio
async def test_create_manual_feed_post_rejects_blank_text() -> None:
    harness = build_messaging_harness()
    container = build_service_container(harness)
    posts = InMemoryFeedPostRepository()
    container.feed_post_repository = posts
    _wire_composer(container, posts)
    character = await create_character(harness)

    response = _client(container).post(
        f"/api/v1/characters/{character.id}/feed",
        json={"content_text": "   "},
    )
    # Pydantic min_length=1 passes whitespace, service-layer entity
    # raises ValueError → 422.
    assert response.status_code == 422


def _wire_memorializer(container, posts, reactions, comments):
    """Mount a real ``FeedReactionMemorializer`` so the seen endpoint
    exercises production code (delta scan + memory write + watermark)."""
    from kokoro_link.application.services.feed_reaction_memorializer import (
        FeedReactionMemorializer,
    )
    from kokoro_link.infrastructure.memory.in_memory import (
        InMemoryMemoryRepository,
    )

    memory_repo = InMemoryMemoryRepository()
    container.feed_reaction_memorializer = FeedReactionMemorializer(
        post_repository=posts,
        reaction_repository=reactions,
        comment_repository=comments,
        memory_repository=memory_repo,
    )
    return memory_repo


@pytest.mark.asyncio
async def test_mark_feed_reactions_seen_returns_404_for_unknown_character() -> None:
    harness = build_messaging_harness()
    container = build_service_container(harness)
    container.feed_post_repository = InMemoryFeedPostRepository()

    response = _client(container).post("/api/v1/characters/ghost/feed/seen")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_mark_feed_reactions_seen_zero_when_no_unseen_interactions() -> None:
    harness = build_messaging_harness()
    container = build_service_container(harness)
    posts = InMemoryFeedPostRepository()
    container.feed_post_repository = posts
    reactions = _wire_reactions(container, posts)
    comments = _wire_comments(container, posts)
    _wire_memorializer(container, posts, reactions, comments)
    character = await create_character(harness)

    response = _client(container).post(
        f"/api/v1/characters/{character.id}/feed/seen",
    )
    assert response.status_code == 200
    assert response.json() == {"updated": 0}


@pytest.mark.asyncio
async def test_mark_feed_reactions_seen_writes_memory_for_new_like() -> None:
    from kokoro_link.domain.entities.feed_reaction import FeedReaction

    harness = build_messaging_harness()
    container = build_service_container(harness)
    posts = InMemoryFeedPostRepository()
    container.feed_post_repository = posts
    reactions = _wire_reactions(container, posts)
    comments = _wire_comments(container, posts)
    memory_repo = _wire_memorializer(container, posts, reactions, comments)
    character = await create_character(harness)
    post = FeedPost.create(
        character_id=character.id,
        kind=FeedKind.MOOD,
        content_text="今天去咖啡廳",
        source=FeedSource.silence(),
        created_at=datetime(2026, 4, 29, 9, 0, tzinfo=timezone.utc),
    )
    await posts.add(post)
    await reactions.add(FeedReaction.create(post_id=post.id))

    response = _client(container).post(
        f"/api/v1/characters/{character.id}/feed/seen",
    )
    assert response.status_code == 200
    assert response.json() == {"updated": 1}
    saved = await memory_repo.query(character.id, limit=10)
    assert len(saved) == 1
    assert "按了讚" in saved[0].content


@pytest.mark.asyncio
async def test_mark_feed_reactions_seen_returns_zero_when_unwired() -> None:
    """Test harnesses without the memorializer still get a 200/zero
    so the frontend's fire-and-forget call doesn't surface noise."""
    harness = build_messaging_harness()
    container = build_service_container(harness)
    container.feed_post_repository = InMemoryFeedPostRepository()
    character = await create_character(harness)

    response = _client(container).post(
        f"/api/v1/characters/{character.id}/feed/seen",
    )
    assert response.status_code == 200
    assert response.json() == {"updated": 0}


@pytest.mark.asyncio
async def test_mark_feed_reactions_seen_resets_unread_reply_count() -> None:
    """The seen endpoint zeroes ``unread_feed_reply_count`` even when
    no memorializer is wired — opening the overlay is the user's
    "I've seen them" signal regardless of the rest of the feed stack."""
    harness = build_messaging_harness()
    container = build_service_container(harness)
    container.feed_post_repository = InMemoryFeedPostRepository()
    character = await create_character(harness)

    # Pretend two replies landed before the user opened the overlay.
    persisted = await harness.character_repository.get(character.id)
    await harness.character_repository.save(
        persisted.with_unread_feed_reply(2),
    )
    persisted = await harness.character_repository.get(character.id)
    assert persisted.unread_feed_reply_count == 2

    response = _client(container).post(
        f"/api/v1/characters/{character.id}/feed/seen",
    )
    assert response.status_code == 200

    after = await harness.character_repository.get(character.id)
    assert after.unread_feed_reply_count == 0


# ---------- Global feed (LumeGram wall) ----------


@pytest.mark.asyncio
async def test_list_global_feed_empty_when_repository_unset() -> None:
    """同 per-character 端點：沒掛 repo 也回 200 / 空頁面，避免前端
    polling 因為未啟用 feed 而炸錯誤。"""
    harness = build_messaging_harness()
    container = build_service_container(harness)
    # feed_post_repository intentionally None

    response = _client(container).get("/api/v1/feed")
    assert response.status_code == 200
    body = response.json()
    assert body == {"items": [], "has_more": False, "next_before": None}


@pytest.mark.asyncio
async def test_list_global_feed_mixes_characters_newest_first() -> None:
    harness = build_messaging_harness()
    container = build_service_container(harness)
    repo = InMemoryFeedPostRepository()
    container.feed_post_repository = repo

    # Multi-user filter: global feed only surfaces the caller's owned
    # characters' posts, so the test characters must exist on the
    # character repo (defaulted to ``user_id=default`` to match the
    # auth-disabled current user).
    char_a = await create_character(harness, name="Mio A")
    char_b = await create_character(harness, name="Mio B")

    base = datetime(2026, 5, 14, 10, 0, tzinfo=timezone.utc)
    await repo.add(FeedPost.create(
        character_id=char_a.id, kind=FeedKind.MOOD,
        content_text="a-old", source=FeedSource.beat("ba1"),
        created_at=base,
    ))
    await repo.add(FeedPost.create(
        character_id=char_b.id, kind=FeedKind.MOOD,
        content_text="b-mid", source=FeedSource.beat("bb1"),
        created_at=base + timedelta(minutes=5),
    ))
    await repo.add(FeedPost.create(
        character_id=char_a.id, kind=FeedKind.MOOD,
        content_text="a-new", source=FeedSource.beat("ba2"),
        created_at=base + timedelta(minutes=10),
    ))

    response = _client(container).get("/api/v1/feed?limit=10")
    assert response.status_code == 200
    body = response.json()
    assert [item["content_text"] for item in body["items"]] == [
        "a-new", "b-mid", "a-old",
    ]
    # character_id 也要照實吐出來，前端要靠這個對到角色 meta
    assert {item["character_id"] for item in body["items"]} == {
        char_a.id, char_b.id,
    }


@pytest.mark.asyncio
async def test_list_global_feed_walks_back_with_before_cursor() -> None:
    harness = build_messaging_harness()
    container = build_service_container(harness)
    repo = InMemoryFeedPostRepository()
    container.feed_post_repository = repo

    char_a = await create_character(harness, name="Mio A")
    char_b = await create_character(harness, name="Mio B")
    base = datetime(2026, 5, 14, 10, 0, tzinfo=timezone.utc)
    for idx in range(4):
        await repo.add(FeedPost.create(
            character_id=char_a.id if idx % 2 == 0 else char_b.id,
            kind=FeedKind.MOOD,
            content_text=f"p{idx}",
            source=FeedSource.beat(f"bg{idx}"),
            created_at=base + timedelta(minutes=idx),
        ))

    cursor = (base + timedelta(minutes=2)).isoformat()
    response = _client(container).get(
        "/api/v1/feed", params={"limit": 10, "before": cursor},
    )
    body = response.json()
    assert [item["content_text"] for item in body["items"]] == ["p1", "p0"]


@pytest.mark.asyncio
async def test_global_feed_unread_no_since_returns_zero() -> None:
    """初次造訪沒帶 since — 紅點不該預設亮起。"""
    harness = build_messaging_harness()
    container = build_service_container(harness)
    repo = InMemoryFeedPostRepository()
    container.feed_post_repository = repo
    character = await create_character(harness, name="Mio C")
    await repo.add(FeedPost.create(
        character_id=character.id, kind=FeedKind.MOOD,
        content_text="x", source=FeedSource.beat("b1"),
    ))

    response = _client(container).get("/api/v1/feed/unread")
    assert response.status_code == 200
    assert response.json() == {"count": 0}


@pytest.mark.asyncio
async def test_global_feed_unread_counts_strictly_after_since() -> None:
    harness = build_messaging_harness()
    container = build_service_container(harness)
    repo = InMemoryFeedPostRepository()
    container.feed_post_repository = repo

    char_a = await create_character(harness, name="Mio A")
    char_b = await create_character(harness, name="Mio B")
    base = datetime(2026, 5, 14, 10, 0, tzinfo=timezone.utc)
    for idx in range(3):
        await repo.add(FeedPost.create(
            character_id=char_a.id if idx == 0 else char_b.id,
            kind=FeedKind.MOOD,
            content_text=f"p{idx}",
            source=FeedSource.beat(f"bu{idx}"),
            created_at=base + timedelta(minutes=idx),
        ))

    response = _client(container).get(
        "/api/v1/feed/unread", params={"since": base.isoformat()},
    )
    assert response.status_code == 200
    # minutes=1, 2 都 > base；minutes=0 嚴格大於 → 不算
    assert response.json() == {"count": 2}
