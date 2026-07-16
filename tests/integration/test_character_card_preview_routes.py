"""Route-level tests for player character-card preview/browse endpoints."""

from __future__ import annotations

import asyncio
import base64
import json
import struct
import zlib
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from kokoro_link.api.app import create_app
from kokoro_link.application.dto.character_card import (
    CHARACTER_CARD_SCHEMA_VERSION,
    CharacterCardManifest,
    CharacterCardMeta,
    CharacterCardProfile,
)
from kokoro_link.domain.entities.operator_profile import OperatorProfile
from kokoro_link.infrastructure.character_card.packager import pack_character_card

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


class _RouteTranslator:
    def __init__(self) -> None:
        self.calls: list[tuple[CharacterCardProfile, str]] = []

    async def translate_profile(
        self,
        profile: CharacterCardProfile,
        *,
        target_language: str,
    ) -> CharacterCardProfile:
        self.calls.append((profile, target_language))
        return profile.model_copy(
            update={
                "name": "Mio",
                "summary": "A college student working at a cafe.",
                "personality": ["bright"],
            },
        )


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _set_player_primary_language(client: TestClient, language: str) -> None:
    container = client.app.state.container

    async def update() -> None:
        profile = await container.operator_profile_repository.get("player")
        assert profile is not None
        await container.operator_profile_repository.save(
            replace(profile, primary_language=language),
        )

    asyncio.run(update())


def _write_demo_pack(directory: Path, pack_id: str = "demo_mio") -> bytes:
    manifest = CharacterCardManifest(
        schema_version=CHARACTER_CARD_SCHEMA_VERSION,
        card=CharacterCardMeta(
            title="美緒 — 示範角色",
            author="Tester",
            description="示範卡",
            tags=["現代", "示範"],
            note="建議動漫風格 profile",
        ),
        character=CharacterCardProfile(
            name="美緒",
            summary="咖啡廳打工女大生",
            personality=["開朗"],
            interests=["咖啡", "唱歌"],
            world_frame="modern",
            arc_template_ref=None,
        ),
        stage_images=["assets/stage/0.png"],
        bundled_arc_templates=[],
    )
    blob = pack_character_card(
        manifest_json=manifest.model_dump_json(indent=2),
        stage_images=[("assets/stage/0.png", _PNG)],
        arc_templates=[],
    )
    (directory / f"{pack_id}.lumecard").write_bytes(blob)
    return blob


@pytest.fixture
def app_with_demo_card(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[TestClient, str, bytes]]:
    blob = _write_demo_pack(tmp_path)
    monkeypatch.setenv("CHARACTER_CARD_PACK_DIR", str(tmp_path))
    monkeypatch.setenv("KOKORO_AUTH_ENABLED", "true")
    monkeypatch.setenv("KOKORO_DATABASE_URL", "")
    monkeypatch.setenv("KOKORO_DEFAULT_PROVIDER_ID", "fake")
    monkeypatch.setenv(
        "KOKORO_JWT_SECRET",
        "character-card-preview-test-secret-at-least-32-bytes",
    )
    app = create_app()
    container = app.state.container
    operator = OperatorProfile(
        id="player",
        display_name="Player",
        email="player@example.com",
        password_hash="test",
        is_admin=False,
        primary_language="en-US",
    )
    asyncio.run(container.operator_profile_repository.save(operator))
    token = container.jwt_service.encode("player")
    with TestClient(app) as client:
        yield client, token, blob


def test_character_card_listing_includes_preview_image_url(
    app_with_demo_card: tuple[TestClient, str, bytes],
) -> None:
    client, token, _blob = app_with_demo_card

    response = client.get("/api/v1/character-cards", headers=_auth(token))

    assert response.status_code == 200
    body = response.json()
    assert body[0]["pack_id"] == "demo_mio"
    assert body[0]["name"] == "美緒"
    assert body[0]["image_urls"] == [
        "/api/v1/character-cards/demo_mio/images/0",
    ]


def test_character_card_pack_image_endpoint_streams_image(
    app_with_demo_card: tuple[TestClient, str, bytes],
) -> None:
    client, token, _blob = app_with_demo_card

    response = client.get(
        "/api/v1/character-cards/demo_mio/images/0",
        headers=_auth(token),
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"] == "public, max-age=86400"
    assert response.content == _PNG


def test_character_card_pack_image_endpoint_accepts_query_token_for_img_tags(
    app_with_demo_card: tuple[TestClient, str, bytes],
) -> None:
    client, token, _blob = app_with_demo_card

    response = client.get(
        f"/api/v1/character-cards/demo_mio/images/0?access_token={token}",
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content == _PNG


def test_character_card_pack_image_endpoint_returns_404_for_out_of_range(
    app_with_demo_card: tuple[TestClient, str, bytes],
) -> None:
    client, token, _blob = app_with_demo_card

    response = client.get(
        "/api/v1/character-cards/demo_mio/images/1",
        headers=_auth(token),
    )

    assert response.status_code == 404


def test_character_card_preview_upload_does_not_create_character(
    app_with_demo_card: tuple[TestClient, str, bytes],
) -> None:
    client, token, blob = app_with_demo_card
    before = client.get("/api/v1/characters", headers=_auth(token)).json()

    response = client.post(
        "/api/v1/characters/card/preview",
        files={"card": ("demo.lumecard", blob, "application/octet-stream")},
        headers=_auth(token),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "美緒"
    assert body["image_urls"][0].startswith("data:image/png;base64,")
    after = client.get("/api/v1/characters", headers=_auth(token)).json()
    assert after == before == []


def test_character_card_preview_upload_can_translate_to_operator_language(
    app_with_demo_card: tuple[TestClient, str, bytes],
) -> None:
    client, token, blob = app_with_demo_card
    translator = _RouteTranslator()
    client.app.state.container.character_card_import_service._translator = translator
    before = client.get("/api/v1/characters", headers=_auth(token)).json()

    response = client.post(
        "/api/v1/characters/card/preview",
        data={"translate": "true"},
        files={"card": ("demo.lumecard", blob, "application/octet-stream")},
        headers=_auth(token),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["title"] == "Mio"
    assert body["description"] == "A college student working at a cafe."
    assert body["name"] == "Mio"
    assert body["summary"] == "A college student working at a cafe."
    assert body["personality"] == ["bright"]
    assert [call[1] for call in translator.calls] == ["en-US"]
    after = client.get("/api/v1/characters", headers=_auth(token)).json()
    assert after == before == []


def test_character_card_import_accepts_initial_relationship_form_json(
    app_with_demo_card: tuple[TestClient, str, bytes],
) -> None:
    client, token, blob = app_with_demo_card
    client.app.state.container.character_runtime_initializer = None

    response = client.post(
        "/api/v1/characters/import",
        data={
            "initial_relationship": json.dumps({
                "relationship_label": "剛帶入的新朋友",
                "known_context": "玩家確認是從角色卡帶入，先慢慢熟悉。",
                "user_address_name": "小夏",
                "character_address_name": "美緒",
                "tone_distance": "友善但不裝熟",
                "familiarity_boundary": "不可杜撰共同回憶。",
                "schedule_involvement_policy": "invite_required",
            }),
        },
        files={"card": ("demo.lumecard", blob, "application/octet-stream")},
        headers=_auth(token),
    )

    assert response.status_code == 200
    character_id = response.json()["character"]["id"]
    relationship_repo = (
        client.app.state.container
        .character_card_import_service
        ._character_service
        ._relationship_seed_repository
    )
    seed = asyncio.run(relationship_repo.get(character_id, "player"))
    assert seed is not None
    assert seed.relationship_label == "剛帶入的新朋友"
    assert seed.schedule_involvement_policy == "invite_required"


def test_character_card_import_rejects_invalid_initial_relationship_form_json(
    app_with_demo_card: tuple[TestClient, str, bytes],
) -> None:
    client, token, blob = app_with_demo_card

    response = client.post(
        "/api/v1/characters/import",
        data={
            "initial_relationship": json.dumps({
                "relationship_label": "朋友",
                "schedule_involvement_policy": "always_join",
            }),
        },
        files={"card": ("demo.lumecard", blob, "application/octet-stream")},
        headers=_auth(token),
    )

    assert response.status_code == 422


def test_character_card_pack_preview_can_translate_current_card(
    app_with_demo_card: tuple[TestClient, str, bytes],
) -> None:
    client, token, _blob = app_with_demo_card
    translator = _RouteTranslator()
    client.app.state.container.character_card_import_service._translator = translator
    before = client.get("/api/v1/characters", headers=_auth(token)).json()

    response = client.post(
        "/api/v1/character-cards/demo_mio/preview",
        params={"translate": "true"},
        headers=_auth(token),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["pack_id"] == "demo_mio"
    assert body["title"] == "Mio"
    assert body["description"] == "A college student working at a cafe."
    assert body["name"] == "Mio"
    assert body["summary"] == "A college student working at a cafe."
    assert body["personality"] == ["bright"]
    assert body["image_urls"] == [
        "/api/v1/character-cards/demo_mio/images/0",
    ]
    assert [call[1] for call in translator.calls] == ["en-US"]
    after = client.get("/api/v1/characters", headers=_auth(token)).json()
    assert after == before == []


def test_character_card_import_upload_can_translate_to_operator_language(
    app_with_demo_card: tuple[TestClient, str, bytes],
) -> None:
    client, token, blob = app_with_demo_card
    translator = _RouteTranslator()
    client.app.state.container.character_card_import_service._translator = translator

    response = client.post(
        "/api/v1/characters/import",
        data={"translate": "true"},
        files={"card": ("demo.lumecard", blob, "application/octet-stream")},
        headers=_auth(token),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["character"]["name"] == "Mio"
    assert body["character"]["summary"] == "A college student working at a cafe."
    assert body["character"]["personality"] == ["bright"]
    assert [call[1] for call in translator.calls] == ["en-US"]


def test_character_card_pack_install_can_translate_to_operator_language(
    app_with_demo_card: tuple[TestClient, str, bytes],
) -> None:
    client, token, _blob = app_with_demo_card
    translator = _RouteTranslator()
    client.app.state.container.character_card_import_service._translator = translator
    client.app.state.container.character_runtime_initializer = None

    response = client.post(
        "/api/v1/character-cards/demo_mio/install",
        params={"translate": "true"},
        headers=_auth(token),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["character"]["name"] == "Mio"
    assert body["character"]["summary"] == "A college student working at a cafe."
    assert body["character"]["personality"] == ["bright"]
    assert [call[1] for call in translator.calls] == ["en-US"]


def test_character_card_pack_install_can_translate_to_japanese_operator_language(
    app_with_demo_card: tuple[TestClient, str, bytes],
) -> None:
    client, token, _blob = app_with_demo_card
    _set_player_primary_language(client, "ja-JP")
    translator = _RouteTranslator()
    client.app.state.container.character_card_import_service._translator = translator
    client.app.state.container.character_runtime_initializer = None

    response = client.post(
        "/api/v1/character-cards/demo_mio/install",
        params={"translate": "true"},
        headers=_auth(token),
    )

    assert response.status_code == 200
    assert response.json()["character"]["name"] == "Mio"
    assert [call[1] for call in translator.calls] == ["ja-JP"]


def test_character_card_pack_install_accepts_initial_relationship_body(
    app_with_demo_card: tuple[TestClient, str, bytes],
) -> None:
    client, token, _blob = app_with_demo_card
    client.app.state.container.character_runtime_initializer = None

    response = client.post(
        "/api/v1/character-cards/demo_mio/install",
        json={
            "initial_relationship": {
                "relationship_label": "想從朋友開始",
                "known_context": "玩家在安裝前看過角色卡。",
                "user_address_name": "小夏",
                "character_address_name": "美緒",
                "tone_distance": "自然但有分寸",
                "familiarity_boundary": "只知道角色卡內容，不要假裝有共同經歷。",
                "schedule_involvement_policy": "mention_only",
            },
        },
        headers=_auth(token),
    )

    assert response.status_code == 200
    character_id = response.json()["character"]["id"]
    relationship_repo = (
        client.app.state.container
        .character_card_import_service
        ._character_service
        ._relationship_seed_repository
    )
    seed = asyncio.run(relationship_repo.get(character_id, "player"))
    assert seed is not None
    assert seed.relationship_label == "想從朋友開始"
    assert seed.schedule_involvement_policy == "mention_only"


def test_character_card_preview_upload_rejects_non_card(
    app_with_demo_card: tuple[TestClient, str, bytes],
) -> None:
    client, token, _blob = app_with_demo_card

    response = client.post(
        "/api/v1/characters/card/preview",
        files={"card": ("junk.lumecard", b"not a zip", "application/octet-stream")},
        headers=_auth(token),
    )

    assert response.status_code == 400


# --- SillyTavern card import (Phase 3 route sniffing) ------------------

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    length = struct.pack(">I", len(data))
    crc = struct.pack(">I", zlib.crc32(chunk_type + data) & 0xFFFFFFFF)
    return length + chunk_type + data + crc


def _sillytavern_v2_json() -> dict:
    return {
        "spec": "chara_card_v2",
        "spec_version": "2.0",
        "data": {
            "name": "Mio",
            "description": "A cheerful barista who loves latte art.",
            "personality": "warm, energetic",
            "scenario": "You walk into her cafe on a rainy afternoon.",
            "first_mes": "Welcome in!",
            "mes_example": "{{char}}: Order up~",
            "creator": "cafe_author",
            "creator_notes": "Best with an anime image profile.",
            "tags": ["slice-of-life", "modern"],
            "alternate_greetings": ["Oh, you again!"],
            "character_book": {
                "name": "Cafe lore",
                "entries": [{"keys": ["latte"]}, {"keys": ["rain"]}],
            },
        },
    }


def _sillytavern_json_bytes() -> bytes:
    return json.dumps(_sillytavern_v2_json()).encode("utf-8")


def _sillytavern_png_bytes() -> bytes:
    encoded = base64.b64encode(_sillytavern_json_bytes())
    ihdr = _png_chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
    chara = _png_chunk(b"tEXt", b"chara\x00" + encoded)
    iend = _png_chunk(b"IEND", b"")
    return _PNG_SIGNATURE + ihdr + chara + iend


def test_sillytavern_json_preview_reports_source_and_dropped_fields(
    app_with_demo_card: tuple[TestClient, str, bytes],
) -> None:
    client, token, _blob = app_with_demo_card
    before = client.get("/api/v1/characters", headers=_auth(token)).json()

    response = client.post(
        "/api/v1/characters/card/preview",
        files={"card": ("mio.json", _sillytavern_json_bytes(), "application/json")},
        headers=_auth(token),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "Mio"
    assert body["source_format"] == "sillytavern"
    # fake provider → normalizer falls open: raw description becomes summary.
    assert body["summary"] == "A cheerful barista who loves latte art."
    assert "character_book" in body["dropped_fields"]
    assert "greetings" in body["dropped_fields"]
    assert body["suggested_known_context"] == (
        "You walk into her cafe on a rainy afternoon."
    )
    after = client.get("/api/v1/characters", headers=_auth(token)).json()
    assert after == before == []


def test_sillytavern_png_preview_uses_png_as_stage_portrait(
    app_with_demo_card: tuple[TestClient, str, bytes],
) -> None:
    client, token, _blob = app_with_demo_card

    response = client.post(
        "/api/v1/characters/card/preview",
        files={"card": ("mio.png", _sillytavern_png_bytes(), "image/png")},
        headers=_auth(token),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "Mio"
    assert body["source_format"] == "sillytavern"
    assert body["stage_image_count"] == 1
    assert body["image_urls"][0].startswith("data:image/png;base64,")


def test_sillytavern_json_import_creates_character(
    app_with_demo_card: tuple[TestClient, str, bytes],
) -> None:
    client, token, _blob = app_with_demo_card
    client.app.state.container.character_runtime_initializer = None

    response = client.post(
        "/api/v1/characters/import",
        files={"card": ("mio.json", _sillytavern_json_bytes(), "application/json")},
        headers=_auth(token),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["character"]["name"] == "Mio"


def test_sillytavern_import_accepts_initial_relationship_form(
    app_with_demo_card: tuple[TestClient, str, bytes],
) -> None:
    client, token, _blob = app_with_demo_card
    client.app.state.container.character_runtime_initializer = None

    response = client.post(
        "/api/v1/characters/import",
        data={
            "initial_relationship": json.dumps({
                "relationship_label": "剛從角色卡帶入",
                "known_context": "玩家確認過情境，先慢慢熟悉。",
                "user_address_name": "小夏",
                "character_address_name": "Mio",
                "tone_distance": "友善但不裝熟",
                "familiarity_boundary": "不可杜撰共同回憶。",
                "schedule_involvement_policy": "invite_required",
            }),
        },
        files={"card": ("mio.json", _sillytavern_json_bytes(), "application/json")},
        headers=_auth(token),
    )

    assert response.status_code == 200
    character_id = response.json()["character"]["id"]
    relationship_repo = (
        client.app.state.container
        .character_card_import_service
        ._character_service
        ._relationship_seed_repository
    )
    seed = asyncio.run(relationship_repo.get(character_id, "player"))
    assert seed is not None
    assert seed.relationship_label == "剛從角色卡帶入"


def test_non_card_png_without_chara_chunk_is_rejected(
    app_with_demo_card: tuple[TestClient, str, bytes],
) -> None:
    client, token, _blob = app_with_demo_card
    plain_png = (
        _PNG_SIGNATURE
        + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
        + _png_chunk(b"IEND", b"")
    )

    response = client.post(
        "/api/v1/characters/card/preview",
        files={"card": ("plain.png", plain_png, "image/png")},
        headers=_auth(token),
    )

    assert response.status_code == 400


def test_sillytavern_v1_flat_card_is_unsupported(
    app_with_demo_card: tuple[TestClient, str, bytes],
) -> None:
    client, token, _blob = app_with_demo_card
    flat = json.dumps({"name": "Flat", "description": "V1 card"}).encode("utf-8")

    response = client.post(
        "/api/v1/characters/card/preview",
        files={"card": ("flat.json", flat, "application/json")},
        headers=_auth(token),
    )

    assert response.status_code == 422
