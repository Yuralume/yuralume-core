"""Cross-user isolation for feed-wall endpoints (P0-3 in the auth review).

Covers the global feed wall, single-post lookup, like/unlike, comment
list/create/delete — every endpoint that previously trusted the path
``post_id`` without verifying the underlying character was owned by the
caller.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from kokoro_link.api.app import create_app
from kokoro_link.application.dto.character import CreateCharacterRequest
from kokoro_link.domain.entities.feed_post import FeedPost
from kokoro_link.domain.entities.operator_profile import OperatorProfile
from kokoro_link.domain.value_objects.feed_kind import FeedKind
from kokoro_link.domain.value_objects.feed_source import FeedSource


@pytest.fixture
def feed_app(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[TestClient, str, str, str, str, str]]:
    """Build an app with Alice + Bob, each owning one character with a feed post.

    Returns ``(client, alice_token, bob_token, alice_char_id,
    alice_post_id, bob_post_id)``.
    """
    monkeypatch.setenv("KOKORO_AUTH_ENABLED", "true")
    monkeypatch.setenv("KOKORO_DATABASE_URL", "")
    monkeypatch.setenv("KOKORO_DEFAULT_PROVIDER_ID", "fake")
    monkeypatch.setenv(
        "KOKORO_JWT_SECRET",
        "feed-isolation-test-secret-at-least-32-bytes",
    )
    app = create_app()
    container = app.state.container

    alice = OperatorProfile(
        id="alice",
        display_name="Alice",
        email="alice@example.com",
        password_hash="test",
        is_admin=True,
    )
    bob = OperatorProfile(
        id="bob",
        display_name="Bob",
        email="bob@example.com",
        password_hash="test",
        is_admin=False,
    )

    async def seed() -> tuple[str, str, str]:
        await container.operator_profile_repository.save(alice)
        await container.operator_profile_repository.save(bob)
        alice_char = await container.character_service.create_character(
            CreateCharacterRequest(name="Alice Character"),
            user_id="alice",
        )
        bob_char = await container.character_service.create_character(
            CreateCharacterRequest(name="Bob Character"),
            user_id="bob",
        )
        alice_post = FeedPost.create(
            character_id=alice_char.id,
            kind=FeedKind.MOOD,
            content_text="alice post",
            source=FeedSource.beat("alice-1"),
        )
        bob_post = FeedPost.create(
            character_id=bob_char.id,
            kind=FeedKind.MOOD,
            content_text="bob post",
            source=FeedSource.beat("bob-1"),
        )
        await container.feed_post_repository.add(alice_post)
        await container.feed_post_repository.add(bob_post)
        return alice_char.id, alice_post.id, bob_post.id

    alice_char_id, alice_post_id, bob_post_id = asyncio.run(seed())

    bob_token = container.jwt_service.encode("bob")
    alice_token = container.jwt_service.encode("alice")
    with TestClient(app) as client:
        yield (
            client,
            alice_token,
            bob_token,
            alice_char_id,
            alice_post_id,
            bob_post_id,
        )


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_global_feed_only_shows_own_characters(
    feed_app: tuple[TestClient, str, str, str, str, str],
) -> None:
    client, alice_token, bob_token, _ac, alice_post_id, bob_post_id = feed_app

    alice_feed = client.get("/api/v1/feed", headers=_auth(alice_token)).json()
    bob_feed = client.get("/api/v1/feed", headers=_auth(bob_token)).json()

    alice_post_ids = {item["id"] for item in alice_feed["items"]}
    bob_post_ids = {item["id"] for item in bob_feed["items"]}

    assert alice_post_id in alice_post_ids
    assert bob_post_id not in alice_post_ids
    assert bob_post_id in bob_post_ids
    assert alice_post_id not in bob_post_ids


def test_global_unread_only_counts_own_characters(
    feed_app: tuple[TestClient, str, str, str, str, str],
) -> None:
    client, alice_token, bob_token, *_ = feed_app
    since = "2020-01-01T00:00:00+00:00"

    alice_count = client.get(
        "/api/v1/feed/unread",
        params={"since": since},
        headers=_auth(alice_token),
    ).json()["count"]
    bob_count = client.get(
        "/api/v1/feed/unread",
        params={"since": since},
        headers=_auth(bob_token),
    ).json()["count"]

    # Each user has exactly one post since 2020.
    assert alice_count == 1
    assert bob_count == 1


def test_bob_cannot_read_alice_feed_post_detail(
    feed_app: tuple[TestClient, str, str, str, str, str],
) -> None:
    client, _alice, bob_token, _ac, alice_post_id, _bp = feed_app
    response = client.get(
        f"/api/v1/feed/posts/{alice_post_id}",
        headers=_auth(bob_token),
    )
    assert response.status_code == 404


def test_bob_cannot_like_alice_feed_post(
    feed_app: tuple[TestClient, str, str, str, str, str],
) -> None:
    client, _alice, bob_token, _ac, alice_post_id, _bp = feed_app
    response = client.post(
        f"/api/v1/feed/posts/{alice_post_id}/like",
        headers=_auth(bob_token),
    )
    assert response.status_code == 404


def test_bob_cannot_unlike_alice_feed_post(
    feed_app: tuple[TestClient, str, str, str, str, str],
) -> None:
    client, _alice, bob_token, _ac, alice_post_id, _bp = feed_app
    response = client.delete(
        f"/api/v1/feed/posts/{alice_post_id}/like",
        headers=_auth(bob_token),
    )
    assert response.status_code == 404


def test_bob_cannot_list_alice_post_comments(
    feed_app: tuple[TestClient, str, str, str, str, str],
) -> None:
    client, _alice, bob_token, _ac, alice_post_id, _bp = feed_app
    response = client.get(
        f"/api/v1/feed/posts/{alice_post_id}/comments",
        headers=_auth(bob_token),
    )
    assert response.status_code == 404


def test_bob_cannot_comment_on_alice_post(
    feed_app: tuple[TestClient, str, str, str, str, str],
) -> None:
    client, _alice, bob_token, _ac, alice_post_id, _bp = feed_app
    response = client.post(
        f"/api/v1/feed/posts/{alice_post_id}/comments",
        headers=_auth(bob_token),
        json={"content_text": "hi alice's character"},
    )
    assert response.status_code == 404


def test_alice_can_like_and_unlike_her_own_post(
    feed_app: tuple[TestClient, str, str, str, str, str],
) -> None:
    client, alice_token, _bob, _ac, alice_post_id, _bp = feed_app

    like = client.post(
        f"/api/v1/feed/posts/{alice_post_id}/like",
        headers=_auth(alice_token),
    )
    assert like.status_code == 200
    assert like.json()["liked"] is True

    unlike = client.delete(
        f"/api/v1/feed/posts/{alice_post_id}/like",
        headers=_auth(alice_token),
    )
    assert unlike.status_code == 200
    assert unlike.json()["liked"] is False


def test_comments_carry_real_user_id_as_author(
    feed_app: tuple[TestClient, str, str, str, str, str],
) -> None:
    client, alice_token, _bob, _ac, alice_post_id, _bp = feed_app
    response = client.post(
        f"/api/v1/feed/posts/{alice_post_id}/comments",
        headers=_auth(alice_token),
        json={"content_text": "first comment"},
    )
    assert response.status_code == 201
    body = response.json()
    # Pre-auth this used to be the literal "local"; multi-user now
    # stamps the comment with the authenticated user id.
    assert body["author_id"] == "alice"
    assert body["author_display_name"] == "Alice"
