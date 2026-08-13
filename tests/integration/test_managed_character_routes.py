"""HTTP contract for managed (IP-partner) characters — EC2-A.

The projection is masked in one place, so the point of these tests is
that *every* character endpoint inherits it: roster, detail, create
response and the PATCH response all come back through the same DTO.
The other half is the write guard — the edit panel PATCHes the whole
form, and a client that has not learned about managed characters must
be refused rather than allowed to save blanks over the persona.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import date

import pytest
from fastapi.testclient import TestClient

from kokoro_link.api.app import create_app
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.operator_profile import DEFAULT_OPERATOR_ID
from kokoro_link.domain.value_objects.character_state import CharacterState


CARD_ID = "cloud-card-mio"


def _managed_character() -> Character:
    return Character.create(
        name="美緒",
        summary="咖啡廳打工的女大生",
        user_id=DEFAULT_OPERATOR_ID,
        personality=["溫柔", "愛操心"],
        interests=["拉花"],
        speaking_style="輕快",
        boundaries=["不談前男友"],
        aspirations=["開自己的店"],
        appearance="black bob cut, apron",
        date_of_birth=date(2003, 4, 18),
        image_urls=["characters/mio/stage-1.png"],
        world_topics=["咖啡"],
        state=CharacterState(
            emotion="calm", affection=42, fatigue=0, trust=50, energy=100,
        ),
        origin_official_card_id=CARD_ID,
    )


@pytest.fixture
def client_with_managed_character(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[TestClient, str]]:
    monkeypatch.setenv("KOKORO_AUTH_ENABLED", "false")
    monkeypatch.setenv("KOKORO_DATABASE_URL", "")
    monkeypatch.setenv("KOKORO_DEFAULT_PROVIDER_ID", "fake")
    monkeypatch.setenv("KOKORO_DEPLOYMENT_MODE", "test")
    monkeypatch.setenv("KOKORO_STORAGE_PROVIDER", "memory")
    app = create_app()
    container = app.state.container
    container.character_runtime_initializer = None
    container.proactive_scheduler = None
    container.world_event_scheduler = None
    container.telegram_polling_service = None

    managed = _managed_character()
    assert container.character_repository is not None
    asyncio.run(container.character_repository.save(managed))

    with TestClient(app) as test_client:
        yield test_client, managed.id


def _stale_full_form(name: str) -> dict[str, object]:
    """What an EC2-B-unaware edit panel sends: every field it rendered,
    which for a managed character means the masked blanks it was given."""
    return {
        "name": name,
        "summary": "咖啡廳打工的女大生",
        "personality": [],
        "interests": [],
        "speaking_style": "",
        "boundaries": [],
        "aspirations": [],
        "appearance": "",
        "gender_identity": "",
        "third_person_pronoun": "",
        "visual_gender_presentation": "",
        "visual_subject_type": "auto",
        "visual_generation_style": "",
        "date_of_birth": None,
        "personality_type": {"system": "mbti_16", "code": ""},
        "companions": [],
    }


def test_roster_and_detail_mask_the_persona(
    client_with_managed_character: tuple[TestClient, str],
) -> None:
    client, character_id = client_with_managed_character

    roster = client.get("/api/v1/characters")
    assert roster.status_code == 200
    entry = next(c for c in roster.json() if c["id"] == character_id)

    detail = client.get(f"/api/v1/characters/{character_id}")
    assert detail.status_code == 200

    for payload in (entry, detail.json()):
        assert payload["managed"] is True
        assert payload["personality"] == []
        assert payload["appearance"] == ""
        assert payload["date_of_birth"] is None
        assert payload["world_topics"] == []
        # The display set a player still needs.
        assert payload["name"] == "美緒"
        assert payload["summary"] == "咖啡廳打工的女大生"
        assert payload["image_urls"] == ["characters/mio/stage-1.png"]
        assert payload["state"]["affection"] == 42


def test_patch_response_is_masked_too(
    client_with_managed_character: tuple[TestClient, str],
) -> None:
    client, character_id = client_with_managed_character

    response = client.patch(
        f"/api/v1/characters/{character_id}",
        json={"proactive_daily_limit": 1, "proactive_cooldown_minutes": 180},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["managed"] is True
    assert payload["personality"] == []
    assert payload["proactive_rhythm"] == "quiet"


def test_stale_client_full_form_patch_is_refused_not_ignored(
    client_with_managed_character: tuple[TestClient, str],
) -> None:
    client, character_id = client_with_managed_character

    response = client.patch(
        f"/api/v1/characters/{character_id}",
        json=_stale_full_form(name="美緒"),
    )

    assert response.status_code == 400
    assert "name" in response.json()["detail"]


def test_refused_patch_leaves_the_persona_intact(
    client_with_managed_character: tuple[TestClient, str],
) -> None:
    client, character_id = client_with_managed_character
    container = client.app.state.container

    client.patch(
        f"/api/v1/characters/{character_id}",
        json=_stale_full_form(name="我改的名字"),
    )

    stored = asyncio.run(container.character_repository.get(character_id))
    assert stored is not None
    assert stored.name == "美緒"
    assert stored.personality == ["溫柔", "愛操心"]
    assert stored.appearance == "black bob cut, apron"
    assert stored.date_of_birth == date(2003, 4, 18)
    assert stored.world_topics == ("咖啡",)


def test_ordinary_character_endpoints_are_unchanged(
    client_with_managed_character: tuple[TestClient, str],
) -> None:
    """The self-host path: no flag, no mask, no new refusals."""
    client, _managed_id = client_with_managed_character

    created = client.post(
        "/api/v1/characters",
        json={"name": "蓮", "personality": ["直率"], "appearance": "tall"},
    )
    assert created.status_code == 201
    assert "managed" not in created.json()
    character_id = created.json()["id"]

    patched = client.patch(
        f"/api/v1/characters/{character_id}",
        json=_stale_full_form(name="蓮改名"),
    )
    assert patched.status_code == 200
    assert "managed" not in patched.json()
    assert patched.json()["name"] == "蓮改名"
    assert patched.json()["personality"] == []
    assert patched.json()["appearance"] == ""
