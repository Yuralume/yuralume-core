"""Cloud→Core showcase internal channel.

``/api/internal/v1/cloud/showcase/**`` — credential-gated with its own
``showcase:read`` scope, never behind the operator JWT. What is covered:

* the gate's three states (unconfigured → 503, wrong credential → 401, a
  credential minted for another scope → 401),
* that a character belonging to another tenant answers **404**, not 403 —
  a distinct answer would be an existence oracle over other people's
  characters, and this endpoint feeds a public marketing page,
* that candidates carry the ``source_hash`` the control plane stores in
  place of the post body.
"""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from kokoro_link.domain.entities.character import Character, CharacterState
from kokoro_link.domain.entities.feed_post import FeedPost
from kokoro_link.domain.entities.operator_profile import OperatorProfile
from kokoro_link.domain.value_objects.feed_kind import FeedKind
from kokoro_link.domain.value_objects.feed_source import FeedSource

_BASE = "/api/internal/v1/cloud/showcase"
_TENANT = "tenant-showcase"
_OTHER_TENANT = "tenant-other"
_CREDENTIALS = (
    "core-kid|cloud-user|yuralume-core|freeze:write,tier:write,showcase:read|core-secret"
)


def _client(monkeypatch, *, credentials: str | None = _CREDENTIALS) -> TestClient:
    monkeypatch.setenv("KOKORO_DATABASE_URL", "")
    monkeypatch.setenv("KOKORO_AUTH_ENABLED", "false")
    monkeypatch.setenv("KOKORO_DEPLOYMENT_MODE", "test")
    monkeypatch.setenv("KOKORO_STORAGE_PROVIDER", "memory")
    monkeypatch.setenv("CONFIG_ENCRYPTION_KEY", "unit-test-showcase-key")
    monkeypatch.delenv("KOKORO_CLOUD_INTERNAL_TOKENS", raising=False)
    if credentials is None:
        monkeypatch.delenv("KOKORO_CLOUD_INTERNAL_CREDENTIALS", raising=False)
    else:
        monkeypatch.setenv("KOKORO_CLOUD_INTERNAL_CREDENTIALS", credentials)

    from kokoro_link.api.app import create_app

    return TestClient(create_app())


def _headers(scope: str = "showcase:read") -> dict[str, str]:
    return {
        "X-Yuralume-Service-Token": "core-secret",
        "X-Yuralume-Service-Key-Id": "core-kid",
        "X-Yuralume-Service-Caller": "cloud-user",
        "X-Yuralume-Service-Audience": "yuralume-core",
        "X-Yuralume-Service-Scope": scope,
    }


def _seed(client: TestClient, *, tenant: str = _TENANT) -> str:
    """One cloud operator under ``tenant``, one character, one post."""
    container = client.app.state.container
    operator = OperatorProfile(
        id=f"cloud:acct-{tenant}",
        display_name="Owner",
        cloud_account_id=f"acct-{tenant}",
        cloud_tenant_id=tenant,
        auth_provider="cloud",
    )
    asyncio.run(container.operator_profile_repository.save(operator))

    character = Character.create(
        name="米菈",
        summary="住在海邊的插畫家",
        user_id=operator.id,
        personality=[],
        interests=[],
        speaking_style="",
        boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
    )
    asyncio.run(container.character_repository.save(character))

    repository = container.feed_post_repository
    if repository is not None:
        asyncio.run(
            repository.add(
                FeedPost.create(
                    id=f"post-{tenant}",
                    character_id=character.id,
                    kind=FeedKind.DAILY,
                    content_text="今天的天空很藍。",
                    source=FeedSource.memory("mem-1"),
                    image_url="/v1/public/feed/default.png",
                ),
            ),
        )
    return character.id


def test_unconfigured_channel_is_503(monkeypatch) -> None:
    client = _client(monkeypatch, credentials=None)

    response = client.get(
        f"{_BASE}/characters", params={"tenant_id": _TENANT}, headers=_headers(),
    )

    assert response.status_code == 503


def test_wrong_secret_is_401(monkeypatch) -> None:
    client = _client(monkeypatch)
    headers = _headers() | {"X-Yuralume-Service-Token": "not-the-secret"}

    response = client.get(
        f"{_BASE}/characters", params={"tenant_id": _TENANT}, headers=headers,
    )

    assert response.status_code == 401


def test_a_tier_scoped_credential_cannot_read_a_feed(monkeypatch) -> None:
    """Scope separation is the point: moving subscription state and reading
    a character's posts are different powers."""
    client = _client(monkeypatch)

    response = client.get(
        f"{_BASE}/candidates",
        params={"tenant_id": _TENANT, "character_id": "char-1"},
        headers=_headers(scope="tier:write"),
    )

    assert response.status_code == 401


def test_candidates_carry_the_source_hash_the_control_plane_stores(
    monkeypatch,
) -> None:
    client = _client(monkeypatch)
    character_id = _seed(client)

    response = client.get(
        f"{_BASE}/candidates",
        params={"tenant_id": _TENANT, "character_id": character_id},
        headers=_headers(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["character"]["id"] == character_id
    candidate = body["candidates"][0]
    assert candidate["text"] == "今天的天空很藍。"
    assert len(candidate["source_hash"]) == 64


def test_review_reports_text_and_image_verdicts(monkeypatch) -> None:
    """Each review row carries both advisory verdicts. The test deployment
    runs the fake model, so nothing actually looked at the post — and both
    verdicts must say so rather than answer "pass"."""
    client = _client(monkeypatch)
    character_id = _seed(client)

    response = client.post(
        f"{_BASE}/review",
        json={
            "tenant_id": _TENANT,
            "character_id": character_id,
            "post_ids": [f"post-{_TENANT}"],
        },
        headers=_headers(),
    )

    assert response.status_code == 200
    review = response.json()["reviews"][0]
    assert review["post_id"] == f"post-{_TENANT}"
    assert {"verdict", "reasons", "rewrite", "image_verdict", "image_reasons"} <= set(review)
    assert review["verdict"] == "needs_manual_review"
    assert review["image_verdict"] == "needs_manual_review"
    assert review["image_reasons"]


def test_another_tenants_character_is_404(monkeypatch) -> None:
    client = _client(monkeypatch)
    character_id = _seed(client, tenant=_OTHER_TENANT)
    _seed(client, tenant=_TENANT)

    response = client.get(
        f"{_BASE}/candidates",
        params={"tenant_id": _TENANT, "character_id": character_id},
        headers=_headers(),
    )

    assert response.status_code == 404


def test_characters_listing_is_scoped_to_the_tenant(monkeypatch) -> None:
    client = _client(monkeypatch)
    mine = _seed(client, tenant=_TENANT)
    theirs = _seed(client, tenant=_OTHER_TENANT)

    response = client.get(
        f"{_BASE}/characters", params={"tenant_id": _TENANT}, headers=_headers(),
    )

    assert response.status_code == 200
    ids = [item["id"] for item in response.json()["characters"]]
    assert mine in ids
    assert theirs not in ids
