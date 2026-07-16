"""BDD-style tests for the operator profile entity + service +
in-memory repository round-trip. Phase 1 of the world-system roadmap.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from kokoro_link.application.services.operator_profile_service import (
    OperatorProfileService,
)
from kokoro_link.application.dto.operator import (
    OperatorProfileResponse,
    UpdateOperatorProfileRequest,
)
from kokoro_link.domain.entities.operator_profile import (
    DEFAULT_OPERATOR_DISPLAY_NAME,
    DEFAULT_OPERATOR_ID,
    OperatorProfile,
)
from kokoro_link.domain.value_objects.actor import Actor, ParticipantRef
from kokoro_link.infrastructure.repositories.in_memory_operator_profile import (
    InMemoryOperatorProfileRepository,
)


def test_default_profile_has_placeholder_name_and_no_real_name_flag():
    profile = OperatorProfile.default()
    assert profile.id == DEFAULT_OPERATOR_ID
    assert profile.display_name == DEFAULT_OPERATOR_DISPLAY_NAME
    assert not profile.has_real_name()


def test_profile_with_explicit_name_reports_real_name():
    profile = OperatorProfile(id="default", display_name="艾力")
    assert profile.has_real_name()
    actor = profile.as_actor()
    assert isinstance(actor, Actor)
    assert actor.kind == "operator"
    assert actor.id == "default"
    assert actor.display_name == "艾力"


def test_profile_update_preserves_unspecified_fields():
    profile = OperatorProfile(
        id="default",
        display_name="艾力",
        aliases=("Alex",),
        pronouns="他",
    )
    updated = profile.update(display_name="阿力")
    assert updated.display_name == "阿力"
    assert updated.aliases == ("Alex",)
    assert updated.pronouns == "他"


def test_profile_timezone_defaults_to_utc() -> None:
    profile = OperatorProfile.default()
    assert profile.timezone_id == "UTC"


def test_profile_update_keeps_timezone_immutable() -> None:
    profile = OperatorProfile(
        id="default", display_name="艾力", timezone_id="Asia/Taipei",
    )
    updated = profile.update(display_name="阿力")
    assert updated.timezone_id == "Asia/Taipei"


def test_profile_rejects_invalid_timezone() -> None:
    with pytest.raises(ValueError, match="timezone"):
        OperatorProfile(id="default", display_name="艾力", timezone_id="local")


def test_profile_update_can_clear_aliases():
    profile = OperatorProfile(
        id="default", display_name="艾力", aliases=("Alex",),
    )
    updated = profile.update(aliases=[])
    assert updated.aliases == ()


def test_profile_update_can_set_preserve_and_clear_current_status():
    profile = OperatorProfile(id="default", display_name="艾力")
    set_at = datetime(2026, 5, 29, 10, 30, tzinfo=timezone.utc)

    updated = profile.update(
        current_status="  今天到學校演講  ",
        current_status_set_at=set_at,
    )

    assert updated.current_status == "今天到學校演講"
    assert updated.current_status_set_at == set_at

    renamed = updated.update(display_name="阿力")
    assert renamed.current_status == "今天到學校演講"
    assert renamed.current_status_set_at == set_at

    cleared = renamed.update(current_status=None)
    assert cleared.current_status is None
    assert cleared.current_status_set_at is None


def test_profile_location_fields_normalise_and_update_tristate():
    profile = OperatorProfile(
        id="default",
        display_name="艾力",
        country_code=" tw ",
        latitude="25.033",  # type: ignore[arg-type]
        longitude="121.5654",  # type: ignore[arg-type]
        location_label="  台北  ",
    )

    assert profile.country_code == "TW"
    assert profile.latitude == 25.033
    assert profile.longitude == 121.5654
    assert profile.location_label == "台北"

    renamed = profile.update(display_name="阿力")
    assert renamed.country_code == "TW"
    assert renamed.latitude == 25.033
    assert renamed.longitude == 121.5654
    assert renamed.location_label == "台北"

    moved = profile.update(
        country_code="us",
        latitude=37.7749,
        longitude=-122.4194,
        location_label=" San Francisco, US ",
    )
    assert moved.country_code == "US"
    assert moved.latitude == 37.7749
    assert moved.longitude == -122.4194
    assert moved.location_label == "San Francisco, US"

    cleared = moved.update(
        country_code=None,
        latitude=None,
        longitude=None,
        location_label=None,
    )
    assert cleared.country_code is None
    assert cleared.latitude is None
    assert cleared.longitude is None
    assert cleared.location_label is None


def test_profile_cloud_identity_fields_normalise_and_update():
    profile = OperatorProfile(
        id="cloud:acct_1",
        display_name="Player",
        cloud_account_id=" acct_1 ",
        cloud_tenant_id=" tenant_1 ",
        cloud_tenant_tier=" DEMO ",
        auth_provider="CLOUD",
    )

    assert profile.cloud_account_id == "acct_1"
    assert profile.cloud_tenant_id == "tenant_1"
    assert profile.cloud_tenant_tier == "demo"
    assert profile.auth_provider == "cloud"

    updated = profile.update(cloud_tenant_id="tenant_2", cloud_tenant_tier="standard")
    assert updated.cloud_account_id == "acct_1"
    assert updated.cloud_tenant_id == "tenant_2"
    assert updated.cloud_tenant_tier == "standard"


def test_profile_rejects_unknown_auth_provider():
    with pytest.raises(ValueError, match="auth provider"):
        OperatorProfile(
            id="operator-1",
            display_name="Player",
            auth_provider="ldap",
        )


@pytest.mark.asyncio
async def test_in_memory_repo_finds_cloud_projection():
    repo = InMemoryOperatorProfileRepository()
    profile = OperatorProfile(
        id="cloud:acct_1",
        display_name="Player",
        cloud_account_id="acct_1",
        cloud_tenant_id="tenant_1",
        auth_provider="cloud",
    )
    await repo.save(profile)

    assert await repo.get_by_cloud_account_id("acct_1") == profile
    assert await repo.get_by_cloud_account_id("missing") is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("country_code", "USA"),
        ("latitude", 91),
        ("longitude", 181),
    ],
)
def test_profile_rejects_malformed_location_fields(field: str, value: object):
    kwargs = {
        "id": "default",
        "display_name": "艾力",
        field: value,
    }
    with pytest.raises(ValueError):
        OperatorProfile(**kwargs)  # type: ignore[arg-type]


def test_operator_profile_dto_exposes_current_status_and_field_presence():
    set_at = datetime(2026, 5, 29, 10, 30, tzinfo=timezone.utc)
    profile = OperatorProfile(
        id="default",
        display_name="艾力",
        current_status="今天到學校演講",
        current_status_set_at=set_at,
        country_code="TW",
        latitude=25.033,
        longitude=121.565,
        location_label="台北",
    )

    response = OperatorProfileResponse.from_domain(profile)
    assert response.current_status == "今天到學校演講"
    assert response.current_status_set_at == set_at
    assert response.country_code == "TW"
    assert response.latitude == 25.033
    assert response.longitude == 121.565
    assert response.location_label == "台北"

    untouched = UpdateOperatorProfileRequest()
    assert "current_status" not in untouched.model_fields_set
    assert "country_code" not in untouched.model_fields_set

    clear = UpdateOperatorProfileRequest(current_status=None)
    assert "current_status" in clear.model_fields_set

    clear_location = UpdateOperatorProfileRequest(country_code=None)
    assert "country_code" in clear_location.model_fields_set


def test_blank_display_name_rejected():
    with pytest.raises(ValueError):
        OperatorProfile(id="default", display_name="   ")


@pytest.mark.asyncio
async def test_service_returns_default_when_repository_empty():
    repo = InMemoryOperatorProfileRepository()
    service = OperatorProfileService(repository=repo)
    profile = await service.get_current()
    assert profile.id == DEFAULT_OPERATOR_ID
    assert not profile.has_real_name()


@pytest.mark.asyncio
async def test_service_round_trips_update():
    repo = InMemoryOperatorProfileRepository()
    service = OperatorProfileService(repository=repo)
    saved = await service.update_default(
        display_name="艾力", aliases=["Alex", "艾"], pronouns="他",
    )
    assert saved.has_real_name()
    fetched = await service.get_current()
    assert fetched.display_name == "艾力"
    assert fetched.aliases == ("Alex", "艾")
    assert fetched.pronouns == "他"
    assert fetched.timezone_id == "UTC"


@pytest.mark.asyncio
async def test_service_update_preserves_other_fields():
    repo = InMemoryOperatorProfileRepository()
    service = OperatorProfileService(repository=repo)
    await service.update_default(display_name="艾力", pronouns="他")
    await service.update_default(display_name="阿力")
    fetched = await service.get_current()
    assert fetched.display_name == "阿力"
    assert fetched.pronouns == "他"


@pytest.mark.asyncio
async def test_service_update_for_user_sets_and_clears_current_status():
    repo = InMemoryOperatorProfileRepository()
    service = OperatorProfileService(repository=repo)

    saved = await service.update_for_user(
        "user-1",
        display_name="艾力",
        current_status="今天到學校演講",
    )

    assert saved.current_status == "今天到學校演講"
    assert saved.current_status_set_at is not None

    cleared = await service.update_for_user("user-1", current_status=None)
    assert cleared.display_name == "艾力"
    assert cleared.current_status is None
    assert cleared.current_status_set_at is None


@pytest.mark.asyncio
async def test_service_update_for_user_sets_and_clears_location():
    repo = InMemoryOperatorProfileRepository()
    service = OperatorProfileService(repository=repo)

    saved = await service.update_for_user(
        "user-1",
        display_name="艾力",
        country_code="us",
        latitude=37.7749,
        longitude=-122.4194,
        location_label="San Francisco, US",
    )

    assert saved.country_code == "US"
    assert saved.latitude == 37.7749
    assert saved.longitude == -122.4194
    assert saved.location_label == "San Francisco, US"

    renamed = await service.update_for_user("user-1", display_name="Alex")
    assert renamed.country_code == "US"
    assert renamed.latitude == 37.7749
    assert renamed.longitude == -122.4194
    assert renamed.location_label == "San Francisco, US"

    cleared = await service.update_for_user(
        "user-1",
        country_code=None,
        latitude=None,
        longitude=None,
        location_label=None,
    )
    assert cleared.display_name == "Alex"
    assert cleared.country_code is None
    assert cleared.latitude is None
    assert cleared.longitude is None
    assert cleared.location_label is None


def test_participant_ref_round_trips_through_dict():
    ref = ParticipantRef(
        actor_kind="character",
        actor_id="char-uuid-1",
        display_name="B",
        role="speaker",
    )
    payload = ref.to_dict()
    decoded = ParticipantRef.from_dict(payload)
    assert decoded == ref


def test_participant_ref_from_dict_drops_malformed():
    assert ParticipantRef.from_dict({"actor_kind": "ghost", "display_name": "X"}) is None
    assert ParticipantRef.from_dict({"actor_kind": "operator", "display_name": ""}) is None
    assert ParticipantRef.from_dict({"display_name": "X"}) is None


def test_participant_ref_accepts_null_actor_id_for_unresolved_npc():
    ref = ParticipantRef(
        actor_kind="npc", actor_id=None, display_name="同事 K",
    )
    assert ref.actor_id is None
    payload = ref.to_dict()
    assert payload["actor_id"] is None


def test_actor_from_operator_profile_carries_aliases():
    profile = OperatorProfile(
        id="op-1", display_name="艾力", aliases=("Alex",),
    )
    actor = profile.as_actor()
    assert actor.aliases == ("Alex",)
