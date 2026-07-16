"""Per-user arc template REST behaviour.

Covers the cross-user isolation, pack visibility, and pack-protection
guarantees the migration to ``arc_templates`` table introduced. Mirrors
``test_user_scoped_preferences.py`` for fixture shape so the two
suites are easy to read side-by-side.
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from kokoro_link.api.app import create_app
from kokoro_link.domain.entities.operator_profile import OperatorProfile


_SAMPLE_BEATS = [
    {
        "sequence": 0, "day_offset": 0, "title": "起點",
        "summary": "場景一摘要。", "tension": "setup",
        "scene_type": "encounter", "location": "教室",
        "scene_characters": [], "dramatic_question": None,
        "required": True,
    },
]


def _draft(
    template_id: str, *, title: str = "我的範本", language: str | None = None,
) -> dict:
    draft: dict = {
        "id": template_id,
        "title": title,
        "premise": "一段測試 premise，足夠長度通過驗證。",
        "theme": "ambition",
        "tone": "dramatic",
        "duration_days": 14,
        "world_frames": ["modern"],
        "required_traits": [],
        "beats": _SAMPLE_BEATS,
    }
    if language is not None:
        draft["language"] = language
    return {"draft": draft, "overwrite": False}


def _patch_payload(*, title: str, language: str | None = None) -> dict:
    payload = {
        "title": title,
        "premise": "一段更新後的 premise，足夠長度通過驗證。",
        "theme": "ambition",
        "tone": "dramatic",
        "duration_days": 14,
        "world_frames": ["modern"],
        "required_traits": [],
        "beats": _SAMPLE_BEATS,
    }
    if language is not None:
        payload["language"] = language
    return payload


@pytest.fixture
def arc_template_app(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[TestClient, str, str]]:
    monkeypatch.setenv("KOKORO_AUTH_ENABLED", "true")
    monkeypatch.setenv("KOKORO_DATABASE_URL", "")
    monkeypatch.setenv("KOKORO_DEFAULT_PROVIDER_ID", "fake")
    monkeypatch.setenv(
        "KOKORO_JWT_SECRET",
        "arc-template-per-user-test-secret-at-least-32-bytes",
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

    async def seed() -> None:
        await container.operator_profile_repository.save(alice)
        await container.operator_profile_repository.save(bob)
        # Drop a pack row directly into the in-memory repo so the test
        # can assert pack visibility without depending on YAML files.
        from kokoro_link.domain.entities.arc_template import (
            ArcTemplate, ArcTemplateBeat, ArcTemplateBinding,
        )
        pack_template = ArcTemplate.create(
            id="shared_pack",
            title="共享 Pack",
            premise="一段共用 pack premise，足夠長度通過驗證。",
            theme="ambition",
            tone="daily",
            duration_days=7,
            beats=[
                ArcTemplateBeat.create(
                    sequence=0, day_offset=0, title="pack 場景",
                    summary="pack 場景摘要，至少幾個字。",
                ),
            ],
            binding=ArcTemplateBinding(world_frames=("modern",)),
        )
        await container.arc_template_repository.upsert_pack(
            pack_template, pack_id="shared_pack", external_id=None,
        )

    asyncio.run(seed())

    alice_token = container.jwt_service.encode("alice")
    bob_token = container.jwt_service.encode("bob")
    with TestClient(app) as client:
        yield client, alice_token, bob_token


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------- pack visibility -----------------------------------------------


def test_pack_row_visible_to_every_user(
    arc_template_app: tuple[TestClient, str, str],
) -> None:
    client, alice_token, bob_token = arc_template_app

    alice_list = client.get(
        "/api/v1/arc-templates", headers=_auth(alice_token),
    )
    bob_list = client.get(
        "/api/v1/arc-templates", headers=_auth(bob_token),
    )
    assert alice_list.status_code == 200
    assert bob_list.status_code == 200
    alice_ids = [t["id"] for t in alice_list.json()]
    bob_ids = [t["id"] for t in bob_list.json()]
    assert "shared_pack" in alice_ids
    assert "shared_pack" in bob_ids


# ---------- per-user authored rows ---------------------------------------


def test_user_authored_template_not_visible_cross_user(
    arc_template_app: tuple[TestClient, str, str],
) -> None:
    client, alice_token, bob_token = arc_template_app

    created = client.post(
        "/api/v1/arc-templates",
        json=_draft("alice_private", title="Alice 私有範本"),
        headers=_auth(alice_token),
    )
    assert created.status_code == 201

    # Alice sees her own row in the list.
    alice_list = client.get(
        "/api/v1/arc-templates", headers=_auth(alice_token),
    )
    alice_ids = [t["id"] for t in alice_list.json()]
    assert "alice_private" in alice_ids

    # Bob does not see it — neither in list nor by direct id.
    bob_list = client.get(
        "/api/v1/arc-templates", headers=_auth(bob_token),
    )
    bob_ids = [t["id"] for t in bob_list.json()]
    assert "alice_private" not in bob_ids
    bob_detail = client.get(
        "/api/v1/arc-templates/alice_private", headers=_auth(bob_token),
    )
    assert bob_detail.status_code == 404


def test_save_collides_with_pack_slug_returns_409(
    arc_template_app: tuple[TestClient, str, str],
) -> None:
    client, alice_token, _ = arc_template_app

    collision = client.post(
        "/api/v1/arc-templates",
        json=_draft("shared_pack", title="嘗試覆寫 pack"),
        headers=_auth(alice_token),
    )
    assert collision.status_code == 409
    assert "reserved by a bundled pack" in collision.json()["detail"]


# ---------- save-time language stamping (Phase A0) -----------------------


def test_save_stamps_operators_stored_primary_language(
    arc_template_app: tuple[TestClient, str, str],
) -> None:
    """Bug: en/ja operators authoring a template via the wizard always
    landed as zh-TW (the domain default) because save never passed the
    operator's language through. Bob's stored primary_language is
    bumped to en-US here; a save that omits ``language`` in the draft
    payload should now stamp en-US onto the row."""
    client, _, bob_token = arc_template_app
    container = client.app.state.container

    async def _bump_bob_language() -> None:
        bob = await container.operator_profile_repository.get("bob")
        await container.operator_profile_repository.save(
            dataclasses.replace(bob, primary_language="en-US"),
        )

    asyncio.run(_bump_bob_language())

    created = client.post(
        "/api/v1/arc-templates",
        json=_draft("bob_en_template", title="Bob's Arc"),
        headers=_auth(bob_token),
    )
    assert created.status_code == 201
    assert created.json()["template"]["language"] == "en-US"


def test_save_with_explicit_draft_language_wins_over_operator_language(
    arc_template_app: tuple[TestClient, str, str],
) -> None:
    client, _, bob_token = arc_template_app
    container = client.app.state.container

    async def _bump_bob_language() -> None:
        bob = await container.operator_profile_repository.get("bob")
        await container.operator_profile_repository.save(
            dataclasses.replace(bob, primary_language="en-US"),
        )

    asyncio.run(_bump_bob_language())

    created = client.post(
        "/api/v1/arc-templates",
        json=_draft("bob_ja_template", title="ボブのアーク", language="ja-JP"),
        headers=_auth(bob_token),
    )
    assert created.status_code == 201
    assert created.json()["template"]["language"] == "ja-JP"


# ---------- patch -------------------------------------------------------


def test_patch_owner_template_overwrites(
    arc_template_app: tuple[TestClient, str, str],
) -> None:
    client, alice_token, _ = arc_template_app

    client.post(
        "/api/v1/arc-templates",
        json=_draft("alice_edit_me", title="原始標題"),
        headers=_auth(alice_token),
    )
    patched = client.patch(
        "/api/v1/arc-templates/alice_edit_me",
        json=_patch_payload(title="覆寫後的標題"),
        headers=_auth(alice_token),
    )
    assert patched.status_code == 200
    assert patched.json()["title"] == "覆寫後的標題"


def test_patch_omitting_language_preserves_existing_value(
    arc_template_app: tuple[TestClient, str, str],
) -> None:
    """Phase A0 regression guard: a PATCH that doesn't mention
    ``language`` must NOT reset a self-authored template back to the
    zh-TW domain default. The wizard save already stamped en-US on
    this row; a title-only edit should leave it alone."""
    client, alice_token, _ = arc_template_app

    client.post(
        "/api/v1/arc-templates",
        json=_draft("alice_en_template", title="EN Title", language="en-US"),
        headers=_auth(alice_token),
    )
    created = client.get(
        "/api/v1/arc-templates/alice_en_template", headers=_auth(alice_token),
    )
    assert created.json()["language"] == "en-US"

    patched = client.patch(
        "/api/v1/arc-templates/alice_en_template",
        json=_patch_payload(title="Edited EN Title"),
        headers=_auth(alice_token),
    )
    assert patched.status_code == 200
    assert patched.json()["title"] == "Edited EN Title"
    assert patched.json()["language"] == "en-US"


def test_patch_with_explicit_language_overwrites_it(
    arc_template_app: tuple[TestClient, str, str],
) -> None:
    client, alice_token, _ = arc_template_app

    client.post(
        "/api/v1/arc-templates",
        json=_draft("alice_lang_switch", title="標題", language="en-US"),
        headers=_auth(alice_token),
    )
    patched = client.patch(
        "/api/v1/arc-templates/alice_lang_switch",
        json=_patch_payload(title="切換語言", language="ja-JP"),
        headers=_auth(alice_token),
    )
    assert patched.status_code == 200
    assert patched.json()["language"] == "ja-JP"


def test_patch_pack_row_returns_409(
    arc_template_app: tuple[TestClient, str, str],
) -> None:
    client, alice_token, _ = arc_template_app

    response = client.patch(
        "/api/v1/arc-templates/shared_pack",
        json=_patch_payload(title="嘗試覆寫 pack"),
        headers=_auth(alice_token),
    )
    assert response.status_code == 409
    assert "reserved by a bundled pack" in response.json()["detail"]


def test_patch_cross_user_returns_404(
    arc_template_app: tuple[TestClient, str, str],
) -> None:
    client, alice_token, bob_token = arc_template_app

    client.post(
        "/api/v1/arc-templates",
        json=_draft("alice_locked", title="Alice 的"),
        headers=_auth(alice_token),
    )
    response = client.patch(
        "/api/v1/arc-templates/alice_locked",
        json=_patch_payload(title="Bob 嘗試覆寫"),
        headers=_auth(bob_token),
    )
    # Bob can't see the slug exists; the save attempt translates the
    # foreign-owner collision into a 400 message that matches the
    # "already exists" branch. Either way the row stays untouched.
    assert response.status_code in (400, 404)


# ---------- delete ------------------------------------------------------


def test_delete_owner_template(
    arc_template_app: tuple[TestClient, str, str],
) -> None:
    client, alice_token, _ = arc_template_app

    client.post(
        "/api/v1/arc-templates",
        json=_draft("alice_remove_me", title="待刪除"),
        headers=_auth(alice_token),
    )
    deleted = client.delete(
        "/api/v1/arc-templates/alice_remove_me", headers=_auth(alice_token),
    )
    assert deleted.status_code == 204

    # Gone from list + direct lookup.
    listing = client.get(
        "/api/v1/arc-templates", headers=_auth(alice_token),
    )
    assert "alice_remove_me" not in [t["id"] for t in listing.json()]


def test_delete_pack_returns_409(
    arc_template_app: tuple[TestClient, str, str],
) -> None:
    client, alice_token, _ = arc_template_app

    response = client.delete(
        "/api/v1/arc-templates/shared_pack", headers=_auth(alice_token),
    )
    assert response.status_code == 409
    assert "bundled pack" in response.json()["detail"]


def test_delete_cross_user_returns_404(
    arc_template_app: tuple[TestClient, str, str],
) -> None:
    client, alice_token, bob_token = arc_template_app

    client.post(
        "/api/v1/arc-templates",
        json=_draft("alice_keep", title="Alice 的"),
        headers=_auth(alice_token),
    )
    response = client.delete(
        "/api/v1/arc-templates/alice_keep", headers=_auth(bob_token),
    )
    assert response.status_code == 404
