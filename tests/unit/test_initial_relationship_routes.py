"""Route + service tests for the whole-seed edit endpoints.

``GET`` / ``PATCH /characters/{id}/initial-relationship`` open the
creation-time relationship seed for editing. What is pinned here:

* tri-state per field (absent / empty string / value),
* the schedule-policy whitelist rejects at the boundary (422) rather than
  letting the entity's ``ValueError`` become a 500,
* an edit that touches an address name still writes the rename-log event
  and reconciles the learned persona — this surface must not become a
  back door around ``update_names``,
* ownership, upsert-on-missing-seed, and the empty-seed equivalence.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from fastapi import FastAPI
from fastapi.testclient import TestClient

from kokoro_link.api.dependencies import get_container, get_current_user_id
from kokoro_link.api.routes.initial_relationship import router
from kokoro_link.application.services.relationship_names_service import (
    RelationshipNamesService,
)
from kokoro_link.domain.entities.character_operator_relationship_seed import (
    CharacterOperatorRelationshipSeed,
)
from kokoro_link.infrastructure.prompt.initial_relationship import (
    render_arc_planner_relationship_lines,
    render_initial_relationship_seed_lines,
)
from kokoro_link.infrastructure.repositories.in_memory_address_change_log import (
    InMemoryAddressChangeLogRepository,
)

_TEST_USER_ID = "alice"
_PATH = "/api/v1/characters/c1/initial-relationship"


class _SeedRepo:
    def __init__(self, seed=None) -> None:
        self.seed = seed
        self.saves = 0

    async def get(self, character_id, operator_id):
        return self.seed

    async def save(self, seed):
        self.seed = seed
        self.saves += 1

    async def delete_for_character(self, character_id):
        return 0


class _PersonaSpy:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def set_explicit_field_for_operator(self, **kwargs):
        self.calls.append(kwargs)


class _ProjectionSpy:
    def __init__(self) -> None:
        self.invalidated: list[tuple[str, str]] = []

    def invalidate(self, character_id, operator_id) -> None:
        self.invalidated.append((character_id, operator_id))


class _DenyingCharacterService:
    async def get_character_entity(self, character_id, user_id=None):
        return None


@dataclass
class _Container:
    relationship_names_service: RelationshipNamesService | None
    operator_persona_projection_service: object | None = None
    character_service: object | None = None  # None → ownership passes through


@dataclass
class _Fixture:
    container: _Container
    seeds: _SeedRepo
    change_log: InMemoryAddressChangeLogRepository
    persona: _PersonaSpy
    projection: _ProjectionSpy


def _fixture(seed=None, *, character_service=None) -> _Fixture:
    seeds = _SeedRepo(seed)
    change_log = InMemoryAddressChangeLogRepository()
    persona = _PersonaSpy()
    projection = _ProjectionSpy()
    service = RelationshipNamesService(
        seed_repository=seeds,
        change_log_repository=change_log,
        persona_service=persona,
    )
    container = _Container(
        relationship_names_service=service,
        operator_persona_projection_service=projection,
        character_service=character_service,
    )
    return _Fixture(container, seeds, change_log, persona, projection)


def _client(container: _Container) -> TestClient:
    app = FastAPI()
    app.dependency_overrides[get_container] = lambda: container
    app.dependency_overrides[get_current_user_id] = lambda: _TEST_USER_ID
    app.include_router(router, prefix="/api/v1")
    return TestClient(app)


def _seed(**overrides) -> CharacterOperatorRelationshipSeed:
    base = {
        "character_id": "c1",
        "operator_id": _TEST_USER_ID,
        "relationship_label": "青梅竹馬",
        "known_context": "同一條巷子長大",
        "living_arrangement": "隔壁棟",
        "user_address_name": "艾力",
        "character_address_name": "美緒",
        "tone_distance": "熟不拘禮",
        "familiarity_boundary": "不談家裡的事",
        "schedule_involvement_policy": "invite_required",
        "proactive_permission": True,
        "proactive_cadence_hint": "晚上七點後",
        "user_profile_notes": "在意工作壓力",
    }
    base.update(overrides)
    return CharacterOperatorRelationshipSeed(**base)


# --- GET ------------------------------------------------------------- #


def test_get_returns_every_field() -> None:
    fx = _fixture(_seed())
    body = _client(fx.container).get(_PATH).json()

    assert body["has_seed"] is True
    assert body["relationship_label"] == "青梅竹馬"
    assert body["known_context"] == "同一條巷子長大"
    assert body["living_arrangement"] == "隔壁棟"
    assert body["user_address_name"] == "艾力"
    assert body["character_address_name"] == "美緒"
    assert body["tone_distance"] == "熟不拘禮"
    assert body["familiarity_boundary"] == "不談家裡的事"
    assert body["schedule_involvement_policy"] == "invite_required"
    assert body["proactive_permission"] is True
    assert body["proactive_cadence_hint"] == "晚上七點後"
    assert body["user_profile_notes"] == "在意工作壓力"


def test_get_without_seed_returns_blank_form() -> None:
    fx = _fixture(None)
    body = _client(fx.container).get(_PATH).json()

    assert body["has_seed"] is False
    assert body["relationship_label"] == ""
    assert body["schedule_involvement_policy"] == "none"
    assert body["proactive_permission"] is False


def test_get_rejects_someone_elses_character() -> None:
    fx = _fixture(_seed(), character_service=_DenyingCharacterService())
    assert _client(fx.container).get(_PATH).status_code == 404


# --- PATCH tri-state --------------------------------------------------- #


def test_patch_absent_field_is_untouched() -> None:
    fx = _fixture(_seed())
    resp = _client(fx.container).patch(_PATH, json={"tone_distance": "客氣一點"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["tone_distance"] == "客氣一點"
    assert body["relationship_label"] == "青梅竹馬"
    assert body["known_context"] == "同一條巷子長大"
    assert body["proactive_permission"] is True


def test_patch_empty_string_clears_the_field() -> None:
    fx = _fixture(_seed())
    body = _client(fx.container).patch(
        _PATH, json={"known_context": "", "user_profile_notes": ""},
    ).json()

    assert body["known_context"] == ""
    assert body["user_profile_notes"] == ""
    assert body["relationship_label"] == "青梅竹馬"


def test_patch_empty_policy_reverts_to_none() -> None:
    fx = _fixture(_seed())
    body = _client(fx.container).patch(
        _PATH, json={"schedule_involvement_policy": ""},
    ).json()
    assert body["schedule_involvement_policy"] == "none"


def test_patch_sets_proactive_permission_false() -> None:
    fx = _fixture(_seed())
    body = _client(fx.container).patch(
        _PATH, json={"proactive_permission": False},
    ).json()
    assert body["proactive_permission"] is False
    assert body["proactive_cadence_hint"] == "晚上七點後"


def test_patch_unknown_policy_is_422() -> None:
    fx = _fixture(_seed())
    resp = _client(fx.container).patch(
        _PATH, json={"schedule_involvement_policy": "whenever_i_feel_like_it"},
    )
    assert resp.status_code == 422
    # Rejected before anything was written.
    assert fx.seeds.saves == 0


def test_patch_empty_body_is_400() -> None:
    fx = _fixture(_seed())
    assert _client(fx.container).patch(_PATH, json={}).status_code == 400


def test_patch_rejects_someone_elses_character() -> None:
    fx = _fixture(_seed(), character_service=_DenyingCharacterService())
    resp = _client(fx.container).patch(_PATH, json={"tone_distance": "疏遠"})
    assert resp.status_code == 404
    assert fx.seeds.saves == 0


def test_patch_service_unwired_is_503() -> None:
    container = _Container(relationship_names_service=None)
    resp = _client(container).patch(_PATH, json={"tone_distance": "疏遠"})
    assert resp.status_code == 503


# --- PATCH keeps the rename side effects ------------------------------- #


def test_address_change_still_writes_rename_log_and_reconciles_persona() -> None:
    fx = _fixture(_seed())
    body = _client(fx.container).patch(
        _PATH,
        json={"user_address_name": "阿丹", "relationship_label": "室友"},
    ).json()

    assert body["user_address_name"] == "阿丹"
    assert body["relationship_label"] == "室友"

    latest = asyncio.run(
        fx.change_log.latest(
            character_id="c1", operator_id=_TEST_USER_ID, direction="player",
        ),
    )
    assert latest is not None
    assert latest.old_value == "艾力"
    assert latest.new_value == "阿丹"
    assert [c["value"] for c in fx.persona.calls] == ["阿丹"]
    assert fx.projection.invalidated == [("c1", _TEST_USER_ID)]


def test_character_direction_change_writes_its_own_log_event() -> None:
    fx = _fixture(_seed())
    _client(fx.container).patch(_PATH, json={"character_address_name": "美緒姐"})

    latest = asyncio.run(
        fx.change_log.latest(
            character_id="c1", operator_id=_TEST_USER_ID, direction="character",
        ),
    )
    assert latest is not None
    assert latest.new_value == "美緒姐"
    # The character direction is not a persona field.
    assert fx.persona.calls == []


def test_non_address_edit_writes_no_rename_log() -> None:
    fx = _fixture(_seed())
    _client(fx.container).patch(_PATH, json={"tone_distance": "客氣一點"})

    latest = asyncio.run(
        fx.change_log.latest(
            character_id="c1", operator_id=_TEST_USER_ID, direction="player",
        ),
    )
    assert latest is None
    assert fx.persona.calls == []


# --- upsert ------------------------------------------------------------ #


def test_patch_without_existing_seed_creates_the_row() -> None:
    fx = _fixture(None)
    body = _client(fx.container).patch(
        _PATH,
        json={"relationship_label": "同事", "user_address_name": "小林"},
    ).json()

    assert body["has_seed"] is True
    assert body["relationship_label"] == "同事"
    assert fx.seeds.seed is not None
    assert fx.seeds.seed.character_id == "c1"
    assert fx.seeds.seed.operator_id == _TEST_USER_ID
    assert fx.seeds.seed.relationship_label == "同事"
    assert fx.seeds.seed.user_address_name == "小林"


def test_patch_keys_the_seed_to_the_caller_not_the_owner_field() -> None:
    """A seed row is always the caller's own pair."""
    fx = _fixture(None)
    _client(fx.container).patch(_PATH, json={"relationship_label": "同事"})
    assert fx.seeds.seed.operator_id == _TEST_USER_ID


# --- empty-seed equivalence -------------------------------------------- #


def test_clearing_every_field_leaves_an_empty_seed_that_reads_as_no_seed() -> None:
    """Clearing everything leaves the row in place. That is safe only
    because every consumer treats an ``is_empty`` seed exactly like a
    missing one — pinned here so a future consumer that forgets the
    ``is_empty`` check trips this test instead of leaking a half-blank
    relationship block into the prompt."""
    fx = _fixture(_seed())
    body = _client(fx.container).patch(
        _PATH,
        json={
            "relationship_label": "",
            "known_context": "",
            "living_arrangement": "",
            "user_address_name": "",
            "character_address_name": "",
            "tone_distance": "",
            "familiarity_boundary": "",
            "schedule_involvement_policy": "",
            "proactive_permission": False,
            "proactive_cadence_hint": "",
            "user_profile_notes": "",
        },
    ).json()

    assert body["has_seed"] is True
    seed = fx.seeds.seed
    assert seed is not None
    assert seed.is_empty is True
    assert render_initial_relationship_seed_lines(seed) == (
        render_initial_relationship_seed_lines(None)
    )
    assert render_arc_planner_relationship_lines(seed) == (
        render_arc_planner_relationship_lines(None)
    )
    # Both schedule-planner and proactive gates read "none" / False, the
    # same values they use for a missing seed.
    assert seed.schedule_involvement_policy == "none"
    assert seed.proactive_permission is False
