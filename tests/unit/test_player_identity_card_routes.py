"""IC1 — ``/identity-cards`` CRUD.

The assembled-app fixture at the bottom doubles as the wiring pin: every
stub-container test above would still pass if the router were never
mounted or the container never built a ``player_identity_card_service``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kokoro_link.api.app import create_app
from kokoro_link.api.dependencies import get_container, get_current_user_id
from kokoro_link.api.routes.player_identity_card import (
    LIMIT_REACHED_CODE,
    NAME_CONFLICT_CODE,
    NOT_FOUND_CODE,
    router,
)
from kokoro_link.application.services.player_identity_card_service import (
    PlayerIdentityCardService,
)
from kokoro_link.domain.entities.character_operator_relationship_seed import (
    SEED_TEXT_FIELD_MAX_CHARS,
)
from kokoro_link.domain.entities.operator_profile import OperatorProfile
from kokoro_link.domain.entities.player_identity_card import (
    PLAYER_IDENTITY_CARD_CONTENT_FIELDS,
    PLAYER_IDENTITY_CARD_NAME_MAX_CHARS,
    PlayerIdentityCard,
)
from kokoro_link.domain.entities.player_persona_note import (
    PLAYER_PERSONA_NOTE_MAX_CHARS,
)
from kokoro_link.infrastructure.repositories.in_memory_player_identity_cards import (
    InMemoryPlayerIdentityCardRepository,
)

_PATH = "/api/v1/identity-cards"
_FULL_CARD = {
    "name": "上班族的我",
    "relationship_label": "同事",
    "known_context": "我們在同一間事務所上班",
    "living_arrangement": "各自住",
    "user_address_name": "小葉",
    "character_address_name": "澄香",
    "tone_distance": "熟稔",
    "familiarity_boundary": "不談家裡的事",
    "schedule_involvement_policy": "invite_required",
    "proactive_permission": True,
    "proactive_cadence_hint": "一天一次",
    "user_profile_notes": "夜貓子",
    "persona_note": "我是超能力者",
}


@dataclass
class _Container:
    player_identity_card_service: PlayerIdentityCardService | None


def _client(
    container: _Container, *, user_id: str = "alice",
) -> TestClient:
    app = FastAPI()
    app.dependency_overrides[get_container] = lambda: container
    app.dependency_overrides[get_current_user_id] = lambda: user_id
    app.include_router(router, prefix="/api/v1")
    return TestClient(app)


def _wired(
    limit: int = 30,
) -> tuple[_Container, InMemoryPlayerIdentityCardRepository]:
    repository = InMemoryPlayerIdentityCardRepository()
    return (
        _Container(
            player_identity_card_service=PlayerIdentityCardService(
                repository, limit=limit,
            ),
        ),
        repository,
    )


def test_list_is_empty_for_a_new_player() -> None:
    container, _ = _wired()

    resp = _client(container).get(_PATH)

    assert resp.status_code == 200
    assert resp.json() == {"cards": [], "limit": 30}


def test_create_then_list_returns_every_field() -> None:
    container, _ = _wired()
    client = _client(container)

    created = client.post(_PATH, json=_FULL_CARD)

    assert created.status_code == 201, created.text
    body = created.json()
    assert body["id"]
    assert body["operator_id"] == "alice"
    assert body["created_at"] and body["updated_at"]

    listed = client.get(_PATH).json()["cards"]
    assert len(listed) == 1
    for field in PLAYER_IDENTITY_CARD_CONTENT_FIELDS:
        assert listed[0][field] == _FULL_CARD[field], field
    assert listed[0]["name"] == _FULL_CARD["name"]


def test_duplicate_name_is_409_with_the_existing_card_id() -> None:
    container, _ = _wired()
    client = _client(container)
    first = client.post(_PATH, json=_FULL_CARD).json()

    clash = client.post(
        _PATH, json={**_FULL_CARD, "name": "  上班族的我  ", "known_context": "新的"},
    )

    assert clash.status_code == 409
    detail = clash.json()["detail"]
    assert detail["code"] == NAME_CONFLICT_CODE
    assert detail["card_id"] == first["id"]
    # Nothing was written.
    assert client.get(_PATH).json()["cards"][0]["known_context"] == (
        _FULL_CARD["known_context"]
    )


class _RacingRepository(InMemoryPlayerIdentityCardRepository):
    """A store whose name pre-check always comes back "free".

    Stands in for the real interleave: two saves of one new name both
    run ``find_by_name`` before either write commits, so the service's
    check cannot see the collision and only the unique constraint does.
    ``upsert`` is left alone — it is the half that must still refuse.
    """

    async def find_by_name(
        self, *, operator_id: str, name: str,
    ) -> PlayerIdentityCard | None:
        return None


def test_a_lost_name_race_is_409_not_500() -> None:
    """The conflict raised at the write, not at the pre-check, still maps."""
    repository = _RacingRepository()
    container = _Container(
        player_identity_card_service=PlayerIdentityCardService(repository),
    )
    client = _client(container)
    first = client.post(_PATH, json=_FULL_CARD)
    assert first.status_code == 201, first.text

    clash = client.post(_PATH, json={**_FULL_CARD, "known_context": "新的"})

    assert clash.status_code == 409, clash.text
    detail = clash.json()["detail"]
    assert detail["code"] == NAME_CONFLICT_CODE
    assert detail["card_id"] == first.json()["id"]
    assert len(client.get(_PATH).json()["cards"]) == 1


def test_overwrite_replaces_content_in_place() -> None:
    container, _ = _wired()
    client = _client(container)
    first = client.post(_PATH, json=_FULL_CARD).json()

    overwritten = client.post(
        _PATH,
        json={**_FULL_CARD, "known_context": "換了一間", "overwrite": True},
    )

    assert overwritten.status_code == 201, overwritten.text
    assert overwritten.json()["id"] == first["id"]
    assert overwritten.json()["created_at"] == first["created_at"]
    cards = client.get(_PATH).json()["cards"]
    assert len(cards) == 1
    assert cards[0]["known_context"] == "換了一間"


def test_creating_past_the_limit_is_409_with_the_limit_code() -> None:
    container, _ = _wired(limit=2)
    client = _client(container)
    for index in range(2):
        assert client.post(
            _PATH, json={**_FULL_CARD, "name": f"卡{index}"},
        ).status_code == 201

    refused = client.post(_PATH, json={**_FULL_CARD, "name": "第三張"})

    assert refused.status_code == 409
    detail = refused.json()["detail"]
    assert detail["code"] == LIMIT_REACHED_CODE
    assert (detail["current"], detail["limit"]) == (2, 2)
    assert len(client.get(_PATH).json()["cards"]) == 2


def test_list_reports_the_limit_so_the_ui_can_warn_first() -> None:
    container, _ = _wired(limit=7)

    assert _client(container).get(_PATH).json()["limit"] == 7


def test_blank_and_over_length_names_are_422() -> None:
    container, _ = _wired()
    client = _client(container)

    assert client.post(_PATH, json={**_FULL_CARD, "name": "   "}).status_code == 422
    assert client.post(
        _PATH,
        json={
            **_FULL_CARD,
            "name": "名" * (PLAYER_IDENTITY_CARD_NAME_MAX_CHARS + 1),
        },
    ).status_code == 422
    assert client.get(_PATH).json()["cards"] == []


def test_unknown_schedule_policy_is_422() -> None:
    container, _ = _wired()

    resp = _client(container).post(
        _PATH, json={**_FULL_CARD, "schedule_involvement_policy": "whatever"},
    )

    assert resp.status_code == 422


def test_over_length_persona_note_is_422() -> None:
    container, _ = _wired()

    resp = _client(container).post(
        _PATH,
        json={
            **_FULL_CARD,
            "persona_note": "設" * (PLAYER_PERSONA_NOTE_MAX_CHARS + 1),
        },
    )

    assert resp.status_code == 422


def test_over_length_seed_text_is_clipped_exactly_as_character_creation_clips() -> None:
    """A card must accept whatever the creation wizard accepted.

    Character creation clips these fields (``trim_seed_text``) rather
    than refusing them, so a 422 here would mean the player can build
    the character but not save the answers that built it — the wizard
    finishes, then "save as card" fails on text the wizard itself let
    through.
    """
    container, _ = _wired()
    client = _client(container)
    over_long = {
        field: "字" * (limit + 100)
        for field, limit in SEED_TEXT_FIELD_MAX_CHARS.items()
    }

    created = client.post(_PATH, json={**_FULL_CARD, **over_long})

    assert created.status_code == 201, created.text
    body = created.json()
    for field, limit in SEED_TEXT_FIELD_MAX_CHARS.items():
        assert body[field] == "字" * limit, field
    # And it round-trips at the clipped length, not the sent length.
    stored = client.get(_PATH).json()["cards"][0]
    assert len(stored["known_context"]) == SEED_TEXT_FIELD_MAX_CHARS["known_context"]


def test_patch_renames_and_keeps_content() -> None:
    container, _ = _wired()
    client = _client(container)
    created = client.post(_PATH, json=_FULL_CARD).json()

    renamed = client.patch(f"{_PATH}/{created['id']}", json={"name": "社畜的我"})

    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["id"] == created["id"]
    assert renamed.json()["name"] == "社畜的我"
    assert renamed.json()["known_context"] == _FULL_CARD["known_context"]


def test_patch_onto_an_existing_name_is_409() -> None:
    container, _ = _wired()
    client = _client(container)
    first = client.post(_PATH, json=_FULL_CARD).json()
    second = client.post(_PATH, json={**_FULL_CARD, "name": "勇者的我"}).json()

    clash = client.patch(f"{_PATH}/{second['id']}", json={"name": "上班族的我"})

    assert clash.status_code == 409
    assert clash.json()["detail"]["code"] == NAME_CONFLICT_CODE
    assert clash.json()["detail"]["card_id"] == first["id"]


def test_delete_removes_the_card_then_404s() -> None:
    container, _ = _wired()
    client = _client(container)
    created = client.post(_PATH, json=_FULL_CARD).json()

    assert client.delete(f"{_PATH}/{created['id']}").status_code == 204
    assert client.get(_PATH).json()["cards"] == []

    gone = client.delete(f"{_PATH}/{created['id']}")
    assert gone.status_code == 404
    assert gone.json()["detail"]["code"] == NOT_FOUND_CODE


def test_another_operators_card_is_404_never_someone_elses_data() -> None:
    container, _ = _wired()
    alice = _client(container, user_id="alice")
    card_id = alice.post(_PATH, json=_FULL_CARD).json()["id"]

    bob = _client(container, user_id="bob")

    assert bob.get(_PATH).json()["cards"] == []
    assert bob.patch(f"{_PATH}/{card_id}", json={"name": "偷改"}).status_code == 404
    assert bob.delete(f"{_PATH}/{card_id}").status_code == 404
    # Bob may reuse the name — uniqueness is per account.
    assert bob.post(_PATH, json=_FULL_CARD).status_code == 201
    # And Alice's card survived every one of those attempts, unrenamed.
    assert alice.get(_PATH).json()["cards"][0]["name"] == _FULL_CARD["name"]


def test_service_unwired_is_503() -> None:
    client = _client(_Container(player_identity_card_service=None))

    assert client.get(_PATH).status_code == 503
    assert client.post(_PATH, json=_FULL_CARD).status_code == 503
    assert client.patch(f"{_PATH}/x", json={"name": "n"}).status_code == 503
    assert client.delete(f"{_PATH}/x").status_code == 503


@pytest.fixture
def auth_app(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, str]]:
    """The real assembled app with auth on."""
    monkeypatch.setenv("KOKORO_AUTH_ENABLED", "true")
    monkeypatch.setenv("KOKORO_DATABASE_URL", "")
    monkeypatch.setenv("KOKORO_DEFAULT_PROVIDER_ID", "fake")
    monkeypatch.setenv(
        "KOKORO_JWT_SECRET",
        "player-identity-card-route-secret-at-least-32-bytes",
    )
    app = create_app()
    container = app.state.container

    async def seed() -> None:
        await container.operator_profile_repository.save(
            OperatorProfile(
                id="player",
                display_name="Player",
                email="player@example.com",
                password_hash="test",
            ),
        )

    asyncio.run(seed())
    token = container.jwt_service.encode("player")
    with TestClient(app) as client:
        yield client, token


def test_unauthenticated_request_is_rejected(
    auth_app: tuple[TestClient, str],
) -> None:
    client, _token = auth_app

    assert client.get(_PATH).status_code in (401, 403)
    assert client.post(_PATH, json=_FULL_CARD).status_code in (401, 403)


def test_authenticated_round_trip_through_the_assembled_app(
    auth_app: tuple[TestClient, str],
) -> None:
    client, token = auth_app
    headers = {"Authorization": f"Bearer {token}"}

    created = client.post(_PATH, json=_FULL_CARD, headers=headers)

    assert created.status_code == 201, created.text
    listed = client.get(_PATH, headers=headers).json()["cards"]
    assert [card["name"] for card in listed] == [_FULL_CARD["name"]]
    assert listed[0]["persona_note"] == _FULL_CARD["persona_note"]
