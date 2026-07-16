from __future__ import annotations

from datetime import timedelta

import pytest

from kokoro_link.application.services.account_runtime_profile import (
    AccountRuntimeProfileResolver,
)
from kokoro_link.domain.entities.operator_profile import OperatorProfile
from kokoro_link.domain.value_objects.account_runtime_profile import (
    DEFAULT_ACCOUNT_RUNTIME_PROFILE,
    DEMO_ACCOUNT_RUNTIME_PROFILE,
    AccountRuntimeProfile,
)
from kokoro_link.infrastructure.repositories.in_memory_operator_profile import (
    InMemoryOperatorProfileRepository,
)


class _StubTierPort:
    """Records the tiers fetched and returns a fixed profile / None."""

    def __init__(self, profile: AccountRuntimeProfile | None) -> None:
        self._profile = profile
        self.calls: list[str] = []

    async def fetch(self, tier: str) -> AccountRuntimeProfile | None:
        self.calls.append(tier)
        return self._profile


async def _save_cloud_operator(repo, *, tier: str) -> str:
    await repo.save(
        OperatorProfile(
            id="cloud:acct_1",
            display_name="Player",
            cloud_account_id="acct_1",
            cloud_tenant_id="tenant_1",
            cloud_tenant_tier=tier,
            auth_provider="cloud",
        )
    )
    return "cloud:acct_1"


@pytest.mark.asyncio
async def test_resolver_returns_default_without_cloud_projection() -> None:
    repo = InMemoryOperatorProfileRepository()
    resolver = AccountRuntimeProfileResolver(repo)

    assert await resolver.resolve_for_operator("missing") == DEFAULT_ACCOUNT_RUNTIME_PROFILE


@pytest.mark.asyncio
async def test_resolver_maps_cloud_demo_tenant_to_demo_profile() -> None:
    repo = InMemoryOperatorProfileRepository()
    await repo.save(
        OperatorProfile(
            id="cloud:acct_1",
            display_name="Demo Player",
            cloud_account_id="acct_1",
            cloud_tenant_id="tenant_demo",
            cloud_tenant_tier="demo",
            auth_provider="cloud",
        )
    )
    resolver = AccountRuntimeProfileResolver(repo)

    profile = await resolver.resolve_for_operator("cloud:acct_1")

    assert profile == DEMO_ACCOUNT_RUNTIME_PROFILE
    assert profile.max_characters == 1
    assert profile.character_ttl.days == 3
    assert profile.album_generation_enabled is False
    assert profile.strict_no_fallback is True


@pytest.mark.asyncio
async def test_resolver_does_not_treat_local_profile_as_demo() -> None:
    repo = InMemoryOperatorProfileRepository()
    await repo.save(
        OperatorProfile(
            id="default",
            display_name="Local Operator",
            cloud_tenant_tier="demo",
            auth_provider="local",
        )
    )
    resolver = AccountRuntimeProfileResolver(repo)

    assert await resolver.resolve_for_operator("default") == DEFAULT_ACCOUNT_RUNTIME_PROFILE


# --- paid-tier control-plane resolution (plan H2 §9) ---------------------


@pytest.mark.asyncio
async def test_resolver_demo_tier_ignores_control_plane_port() -> None:
    repo = InMemoryOperatorProfileRepository()
    await _save_cloud_operator(repo, tier="demo")
    port = _StubTierPort(AccountRuntimeProfile(name="plus"))
    resolver = AccountRuntimeProfileResolver(repo, tier_profile_port=port)

    profile = await resolver.resolve_for_operator("cloud:acct_1")

    # Demo stays the hardcoded restrictive profile; the port is never consulted.
    assert profile == DEMO_ACCOUNT_RUNTIME_PROFILE
    assert port.calls == []


@pytest.mark.asyncio
async def test_resolver_paid_tier_fetches_from_control_plane_port() -> None:
    repo = InMemoryOperatorProfileRepository()
    await _save_cloud_operator(repo, tier="plus")
    tier_profile = AccountRuntimeProfile(name="plus", max_characters=10)
    port = _StubTierPort(tier_profile)
    resolver = AccountRuntimeProfileResolver(repo, tier_profile_port=port)

    profile = await resolver.resolve_for_operator("cloud:acct_1")

    assert profile is tier_profile
    assert port.calls == ["plus"]


@pytest.mark.asyncio
async def test_resolver_paid_tier_falls_back_to_default_when_port_returns_none() -> None:
    repo = InMemoryOperatorProfileRepository()
    await _save_cloud_operator(repo, tier="plus")
    port = _StubTierPort(None)
    resolver = AccountRuntimeProfileResolver(repo, tier_profile_port=port)

    profile = await resolver.resolve_for_operator("cloud:acct_1")

    assert profile == DEFAULT_ACCOUNT_RUNTIME_PROFILE
    assert port.calls == ["plus"]


@pytest.mark.asyncio
async def test_resolver_paid_tier_defaults_when_port_unwired() -> None:
    repo = InMemoryOperatorProfileRepository()
    await _save_cloud_operator(repo, tier="plus")
    resolver = AccountRuntimeProfileResolver(repo)  # no tier port

    profile = await resolver.resolve_for_operator("cloud:acct_1")

    assert profile == DEFAULT_ACCOUNT_RUNTIME_PROFILE


# --- AccountRuntimeProfile.from_control_plane_payload (plan H2 §5) --------


def test_parser_full_payload_maps_every_knob() -> None:
    profile = AccountRuntimeProfile.from_control_plane_payload("plus", {
        "proactive_tick_multiplier": 3,
        "character_ttl_days": 14,
        "max_characters": 8,
        "daily_character_create_limit": 2,
        "max_messages_per_session": 500,
        "daily_chat_image_limit": 20,
        "daily_feed_post_limit": 5,
        "album_generation_enabled": True,
        "video_generation_enabled": False,
        "tts_enabled": False,
        "strict_no_fallback": True,
        "background_judge_model_pin": "judge-model-x",
    })

    assert profile.name == "plus"
    assert profile.proactive_tick_multiplier == 3
    assert profile.character_ttl == timedelta(days=14)
    assert profile.max_characters == 8
    assert profile.daily_character_create_limit == 2
    assert profile.max_messages_per_session == 500
    assert profile.daily_chat_image_limit == 20
    assert profile.daily_feed_post_limit == 5
    assert profile.album_generation_enabled is True
    assert profile.video_generation_enabled is False
    assert profile.tts_enabled is False
    assert profile.strict_no_fallback is True
    assert profile.background_judge_model_pin == "judge-model-x"


def test_parser_empty_payload_yields_default_knobs_with_tier_name() -> None:
    profile = AccountRuntimeProfile.from_control_plane_payload("plus", {})

    default = DEFAULT_ACCOUNT_RUNTIME_PROFILE
    # Same permissive knobs as DEFAULT, only the name differs.
    assert profile.name == "plus"
    assert profile == AccountRuntimeProfile(
        name="plus",
        proactive_tick_multiplier=default.proactive_tick_multiplier,
        character_ttl=default.character_ttl,
        max_characters=default.max_characters,
        daily_character_create_limit=default.daily_character_create_limit,
        max_messages_per_session=default.max_messages_per_session,
        background_judge_model_pin=default.background_judge_model_pin,
        strict_no_fallback=default.strict_no_fallback,
        daily_chat_image_limit=default.daily_chat_image_limit,
        daily_feed_post_limit=default.daily_feed_post_limit,
        album_generation_enabled=default.album_generation_enabled,
        video_generation_enabled=default.video_generation_enabled,
        tts_enabled=default.tts_enabled,
    )


def test_parser_ignores_invalid_typed_values_per_knob() -> None:
    profile = AccountRuntimeProfile.from_control_plane_payload("plus", {
        "proactive_tick_multiplier": "fast",      # not an int -> default (1)
        "max_characters": 0,                       # below minimum -> default (None)
        "daily_character_create_limit": True,      # bool rejected -> default (None)
        "max_messages_per_session": -5,            # below minimum -> default (None)
        "album_generation_enabled": "yes",         # not a bool -> default (True)
        "background_judge_model_pin": 123,         # not a str -> default (None)
        "tts_enabled": False,                      # valid bool -> honoured
    })

    assert profile.proactive_tick_multiplier == 1
    assert profile.max_characters is None
    assert profile.daily_character_create_limit is None
    assert profile.max_messages_per_session is None
    assert profile.album_generation_enabled is True
    assert profile.background_judge_model_pin is None
    # A single bad knob does not poison the valid ones.
    assert profile.tts_enabled is False


def test_parser_character_ttl_days_mapping_and_invalid() -> None:
    assert AccountRuntimeProfile.from_control_plane_payload(
        "plus", {"character_ttl_days": 7},
    ).character_ttl == timedelta(days=7)
    # Invalid / sub-minimum ttl -> None (no TTL), matching the default.
    assert AccountRuntimeProfile.from_control_plane_payload(
        "plus", {"character_ttl_days": 0},
    ).character_ttl is None
    assert AccountRuntimeProfile.from_control_plane_payload(
        "plus", {"character_ttl_days": "week"},
    ).character_ttl is None


def test_parser_explicit_null_and_unknown_keys() -> None:
    profile = AccountRuntimeProfile.from_control_plane_payload("plus", {
        "max_characters": None,        # nullable -> stays None (unlimited)
        "unknown_future_knob": "x",    # unknown key -> ignored
    })

    assert profile.max_characters is None
    assert profile.name == "plus"


def test_parser_non_dict_payload_is_treated_as_empty() -> None:
    profile = AccountRuntimeProfile.from_control_plane_payload("plus", None)

    assert profile.name == "plus"
    assert profile.max_characters is None
