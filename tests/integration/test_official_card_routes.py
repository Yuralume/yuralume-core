"""Official cards over HTTP: the shape the SPA actually receives.

The catalog itself is faked at the port — no network — but everything in
front of it is real: the route, the envelope, the id round trip and the
status codes. The id round trip is the one that cannot be checked anywhere
else: the frontend treats ``pack_id`` as opaque and sends it back through
``encodeURIComponent``, so a Cloud card's ``cloud:`` prefix arrives
percent-encoded and has to survive the path decode intact.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from kokoro_link.api.app import create_app
from kokoro_link.application.dto.character_card import (
    CHARACTER_CARD_SCHEMA_VERSION,
    CharacterCardManifest,
    CharacterCardMeta,
    CharacterCardProfile,
)
from kokoro_link.application.services.character_card_pack_service import (
    CharacterCardPackService,
)
from kokoro_link.application.services.character_service import (
    CharacterValidationError,
)
from kokoro_link.application.services.official_card_pack_source import (
    OfficialCardPackSource,
)
from kokoro_link.contracts.official_card_catalog import (
    OfficialCardCatalogPort,
    OfficialCardDetail,
    OfficialCardImage,
    OfficialCardSummary,
)
from kokoro_link.domain.entities.operator_profile import OperatorProfile
from kokoro_link.infrastructure.character_card.pack_catalog import (
    CharacterCardPackCatalog,
)
from kokoro_link.infrastructure.character_card.packager import pack_character_card

_CARD_ID = "mio-cafe"
_CLOUD_REF = f"cloud:{_CARD_ID}"
_IMAGE_URL = (
    "https://app.yuralume.com/api/user"
    "/v1/public/official-cards/mio-cafe/images/0"
)


class _FakeCatalog(OfficialCardCatalogPort):
    def __init__(self) -> None:
        self.reachable = True
        # What the artifact endpoint hands back. ``None`` means "a real
        # card"; the tests that matter here set something the importer
        # refuses, which is the one failure a mirror cannot rule out —
        # Cloud publishes the bytes, this build decides if it can read them.
        self.artifact: bytes | None = None

    async def list_cards(self, *, locale: str) -> list[OfficialCardSummary] | None:
        if not self.reachable:
            return None
        return [OfficialCardSummary(
            id=_CARD_ID,
            title="ミオ",
            summary="カフェで働く大学生。",
            tags=("現代",),
            author="Yuralume",
            localized=True,
            image_url=_IMAGE_URL,
            image_count=1,
        )]

    async def get_card(
        self, card_id: str, *, locale: str, fresh: bool = False,
    ) -> OfficialCardDetail | None:
        if not self.reachable:
            return None
        return OfficialCardDetail(
            id=card_id,
            title="ミオ",
            description="公式カード",
            localized=True,
            locale=locale,
            available_locales=("zh-Hant", "en", "ja"),
            profile={"name": "ミオ", "summary": "カフェで働く大学生。"},
            images=(OfficialCardImage(index=0, url=_IMAGE_URL),),
        )

    async def download_artifact(self, card_id: str) -> bytes | None:
        if not self.reachable:
            return None
        return _artifact() if self.artifact is None else self.artifact


def _artifact() -> bytes:
    manifest = CharacterCardManifest(
        card=CharacterCardMeta(title="美緒 — 官方角色", author="Yuralume"),
        character=CharacterCardProfile(name="美緒", summary="咖啡廳打工女大生"),
    )
    return pack_character_card(
        manifest_json=manifest.model_dump_json(indent=2),
        stage_images=[],
        arc_templates=[],
    )


def _artifact_from_a_newer_exporter() -> bytes:
    manifest = json.loads(CharacterCardManifest(
        card=CharacterCardMeta(title="美緒 — 官方角色", author="Yuralume"),
        character=CharacterCardProfile(name="美緒", summary="咖啡廳打工女大生"),
    ).model_dump_json())
    manifest["schema_version"] = CHARACTER_CARD_SCHEMA_VERSION + 1
    return pack_character_card(
        manifest_json=json.dumps(manifest),
        stage_images=[],
        arc_templates=[],
    )


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def app_with_official_catalog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[TestClient, str, _FakeCatalog]]:
    monkeypatch.setenv("CHARACTER_CARD_PACK_DIR", str(tmp_path))
    monkeypatch.setenv("KOKORO_AUTH_ENABLED", "true")
    monkeypatch.setenv("KOKORO_DATABASE_URL", "")
    monkeypatch.setenv("KOKORO_DEFAULT_PROVIDER_ID", "fake")
    monkeypatch.setenv(
        "KOKORO_JWT_SECRET",
        "official-card-route-test-secret-at-least-32-bytes",
    )
    app = create_app()
    container = app.state.container
    container.character_runtime_initializer = None

    # The container wires the real HTTP client from env; swap the port for a
    # fake so the suite never reaches the public catalog.
    catalog = _FakeCatalog()
    import_service = container.character_card_import_service
    container.character_card_pack_service = CharacterCardPackService(
        catalog=CharacterCardPackCatalog(directories=[tmp_path]),
        import_service=import_service,
        official_cards=OfficialCardPackSource(
            catalog=catalog, import_service=import_service,
        ),
    )

    operator = OperatorProfile(
        id="player",
        display_name="Player",
        email="player@example.com",
        password_hash="test",
        is_admin=False,
        primary_language="ja-JP",
    )
    asyncio.run(container.operator_profile_repository.save(operator))
    token = container.jwt_service.encode("player")
    with TestClient(app) as client:
        yield client, token, catalog


def test_the_shelf_lists_official_cards_with_absolute_image_urls(
    app_with_official_catalog: tuple[TestClient, str, _FakeCatalog],
) -> None:
    client, token, _catalog = app_with_official_catalog

    body = client.get("/api/v1/character-cards", headers=_auth(token)).json()

    assert body["official_cards_unavailable"] is False
    card = body["cards"][0]
    assert card["pack_id"] == _CLOUD_REF
    assert card["source"] == "cloud"
    assert card["localized"] is True
    # Absolute: the browser fetches official images from Cloud directly, so
    # this deployment never serves an official thumbnail.
    assert card["image_urls"] == [_IMAGE_URL]


def test_an_official_pack_id_survives_the_url_round_trip(
    app_with_official_catalog: tuple[TestClient, str, _FakeCatalog],
) -> None:
    """``cloud:mio-cafe`` arrives as ``cloud%3Amio-cafe`` and must decode.

    The frontend never parses a ``pack_id``; it sends back exactly what it
    was given, percent-encoded. If the prefix did not survive, every
    official card would 404 on preview and install.
    """
    client, token, _catalog = app_with_official_catalog

    response = client.post(
        f"/api/v1/character-cards/{quote(_CLOUD_REF, safe='')}/preview",
        headers=_auth(token),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["pack_id"] == _CLOUD_REF
    assert body["name"] == "ミオ"
    assert body["source"] == "cloud"


def test_installing_an_official_card_creates_a_local_character(
    app_with_official_catalog: tuple[TestClient, str, _FakeCatalog],
) -> None:
    client, token, _catalog = app_with_official_catalog

    response = client.post(
        f"/api/v1/character-cards/{quote(_CLOUD_REF, safe='')}/install",
        headers=_auth(token),
    )

    assert response.status_code == 200
    character = response.json()["character"]
    assert character["name"] == "ミオ"
    assert client.get(
        f"/api/v1/characters/{character['id']}", headers=_auth(token),
    ).status_code == 200


def test_an_artifact_the_importer_refuses_is_a_4xx_not_a_500(
    app_with_official_catalog: tuple[TestClient, str, _FakeCatalog],
) -> None:
    """The download succeeded; the card is what failed.

    "Cloud is unreachable" (503) and "Cloud answered with something this
    build cannot read" are different states, and neither is an unhandled
    exception. A 500 here tells the player Yuralume broke, and tells the
    owner nothing about which card is unpublishable.
    """
    client, token, catalog = app_with_official_catalog
    catalog.artifact = b"this is not a zip at all"

    response = client.post(
        f"/api/v1/character-cards/{quote(_CLOUD_REF, safe='')}/install",
        headers=_auth(token),
    )

    # Same mapping the manual `.lumecard` upload has always used, so one
    # frontend handler covers both doors into the import pipeline.
    assert response.status_code == 400
    assert response.json()["detail"]


def test_an_artifact_from_a_newer_exporter_is_422_like_a_manual_upload(
    app_with_official_catalog: tuple[TestClient, str, _FakeCatalog],
) -> None:
    """A card this build is too old to read — refused, not half-imported."""
    client, token, catalog = app_with_official_catalog
    catalog.artifact = _artifact_from_a_newer_exporter()

    response = client.post(
        f"/api/v1/character-cards/{quote(_CLOUD_REF, safe='')}/install",
        headers=_auth(token),
    )

    assert response.status_code == 422
    assert "schema_version" in response.json()["detail"]


def test_a_full_character_shelf_refuses_the_install_without_crashing(
    app_with_official_catalog: tuple[TestClient, str, _FakeCatalog],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Installing creates a character, so it meets the account's limits.

    A hosted player at their character-slot cap is an ordinary refusal, not
    an exception — and ``POST /characters`` has always answered it with 400.
    The marketplace door has to say the same thing.
    """
    client, token, _catalog = app_with_official_catalog
    service = client.app.state.container.character_card_pack_service

    async def _at_the_limit(*_args: object, **_kwargs: object) -> None:
        raise CharacterValidationError(
            "account runtime profile character limit reached (3)",
        )

    monkeypatch.setattr(service, "install", _at_the_limit)

    response = client.post(
        f"/api/v1/character-cards/{quote(_CLOUD_REF, safe='')}/install",
        headers=_auth(token),
    )

    assert response.status_code == 400
    assert "limit reached" in response.json()["detail"]


def test_an_unreadable_local_pack_is_a_4xx_on_preview_too(
    app_with_official_catalog: tuple[TestClient, str, _FakeCatalog],
    tmp_path: Path,
) -> None:
    """Preview runs the same unpack + validate, so it has the same door."""
    client, token, _catalog = app_with_official_catalog
    (tmp_path / "corrupt.lumecard").write_bytes(b"not a zip either")

    response = client.post(
        "/api/v1/character-cards/corrupt/preview", headers=_auth(token),
    )

    assert response.status_code == 400
    assert response.json()["detail"]


def test_a_cloud_outage_is_an_empty_shelf_on_list_and_503_on_install(
    app_with_official_catalog: tuple[TestClient, str, _FakeCatalog],
) -> None:
    client, token, catalog = app_with_official_catalog
    catalog.reachable = False

    listing = client.get("/api/v1/character-cards", headers=_auth(token))
    assert listing.status_code == 200
    assert listing.json() == {"cards": [], "official_cards_unavailable": True}

    # Never a 500, and never a 404 that would claim the card was withdrawn.
    install = client.post(
        f"/api/v1/character-cards/{quote(_CLOUD_REF, safe='')}/install",
        headers=_auth(token),
    )
    assert install.status_code == 503
