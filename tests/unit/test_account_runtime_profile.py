from __future__ import annotations

from datetime import timedelta

import pytest

from kokoro_link.application.services.account_runtime_profile import (
    AccountRuntimeProfileResolver,
)
from kokoro_link.domain.entities.operator_profile import OperatorProfile
from kokoro_link.domain.value_objects.account_runtime_profile import (
    BILLING_SHAPE_ACTION_FIXED,
    BILLING_SHAPE_TOKEN_FLOATING,
    DEFAULT_ACCOUNT_RUNTIME_PROFILE,
    DEFAULT_DAILY_OVERAGE_LIMIT,
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
    assert profile.background_activity_multiplier == 6


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
        "background_activity_multiplier": 4,
        "idle_downshift_days": 7,
        "idle_multiplier": 5,
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
    assert profile.background_activity_multiplier == 4
    assert profile.idle_downshift_days == 7
    assert profile.idle_multiplier == 5
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


def test_parser_rejects_frequency_knobs_above_contract_maximum(caplog) -> None:
    profile = AccountRuntimeProfile.from_control_plane_payload("plus", {
        "proactive_tick_multiplier": 289,
        "background_activity_multiplier": 600,
        "idle_multiplier": 289,
        "idle_downshift_days": 3651,
    })

    assert profile.proactive_tick_multiplier == 1
    assert profile.background_activity_multiplier == 1
    assert profile.idle_multiplier == 1
    assert profile.idle_downshift_days is None
    assert "ignoring invalid proactive_tick_multiplier=289" in caplog.text
    assert "ignoring invalid background_activity_multiplier=600" in caplog.text
    assert "ignoring invalid idle_multiplier=289" in caplog.text
    assert "ignoring invalid idle_downshift_days=3651" in caplog.text


def test_effective_multipliers_apply_idle_factor_and_clamp(caplog) -> None:
    profile = AccountRuntimeProfile(
        name="plus",
        proactive_tick_multiplier=49,
        background_activity_multiplier=72,
        idle_downshift_days=7,
        idle_multiplier=6,
    )

    assert profile.effective_proactive_multiplier(idle=False) == 49
    assert profile.effective_background_multiplier(idle=False) == 72
    assert profile.effective_proactive_multiplier(idle=True) == 288
    assert profile.effective_background_multiplier(idle=True) == 288
    assert "clamped effective proactive multiplier" in caplog.text
    assert "clamped effective background multiplier" in caplog.text


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


def test_billing_knobs_default_to_todays_token_shape() -> None:
    """AP0: every un-migrated tier keeps floating token billing and has
    overage off, so the new code paths are opt-in per tier."""
    profile = AccountRuntimeProfile.from_control_plane_payload("plus", {})

    assert profile.billing_shape == BILLING_SHAPE_TOKEN_FLOATING
    assert profile.uses_action_pricing is False
    assert profile.overage_enabled is False
    assert profile.daily_overage_limit == DEFAULT_DAILY_OVERAGE_LIMIT


def test_billing_knobs_parsed_from_the_control_plane() -> None:
    profile = AccountRuntimeProfile.from_control_plane_payload("plus", {
        "billing_shape": "action_fixed",
        "overage_enabled": True,
        "daily_overage_limit": 3,
    })

    assert profile.billing_shape == BILLING_SHAPE_ACTION_FIXED
    assert profile.uses_action_pricing is True
    assert profile.overage_enabled is True
    assert profile.daily_overage_limit == 3


def test_unknown_billing_shape_falls_back_instead_of_passing_through() -> None:
    """An undefined third shape has no behaviour; charging must not guess."""
    profile = AccountRuntimeProfile.from_control_plane_payload(
        "plus", {"billing_shape": "per_vibe"},
    )

    assert profile.billing_shape == BILLING_SHAPE_TOKEN_FLOATING
    assert profile.uses_action_pricing is False


def test_invalid_overage_knobs_fall_back_to_the_safe_defaults() -> None:
    profile = AccountRuntimeProfile.from_control_plane_payload("plus", {
        "overage_enabled": "yes",
        "daily_overage_limit": -1,
    })

    assert profile.overage_enabled is False
    assert profile.daily_overage_limit == DEFAULT_DAILY_OVERAGE_LIMIT


def test_demo_profile_is_not_action_priced() -> None:
    assert DEMO_ACCOUNT_RUNTIME_PROFILE.uses_action_pricing is False
    assert DEMO_ACCOUNT_RUNTIME_PROFILE.overage_enabled is False


# --- story_scene_daily_limit (SC3-B) ---------------------------------------


def test_story_scene_daily_limit_defaults_to_unlimited() -> None:
    profile = AccountRuntimeProfile.from_control_plane_payload("plus", {})

    assert profile.story_scene_daily_limit is None


def test_story_scene_daily_limit_parsed_from_control_plane() -> None:
    profile = AccountRuntimeProfile.from_control_plane_payload("plus", {
        "story_scene_daily_limit": 3,
    })

    assert profile.story_scene_daily_limit == 3


def test_story_scene_daily_limit_rejects_zero_and_negative() -> None:
    """0 is not a second spelling of "unlimited" -- unlike overage_daily_limit
    this knob's CHECK runs the other way (NULL allowed, 0 is not), so an
    invalid 0/negative value falls back to the default (None) exactly like any
    other out-of-range knob, not to some other sentinel."""
    zero = AccountRuntimeProfile.from_control_plane_payload("plus", {
        "story_scene_daily_limit": 0,
    })
    assert zero.story_scene_daily_limit is None

    negative = AccountRuntimeProfile.from_control_plane_payload("plus", {
        "story_scene_daily_limit": -1,
    })
    assert negative.story_scene_daily_limit is None


def test_story_scene_daily_limit_explicit_null_stays_unlimited() -> None:
    profile = AccountRuntimeProfile.from_control_plane_payload("plus", {
        "story_scene_daily_limit": None,
    })

    assert profile.story_scene_daily_limit is None


def test_story_scene_daily_limit_missing_key_is_zero_impact_for_self_host() -> None:
    """self-host never sends this knob at all -- confirms the fail-open default
    stays unlimited with no payload change required."""
    assert DEFAULT_ACCOUNT_RUNTIME_PROFILE.story_scene_daily_limit is None


# --- video_daily_limit (CV5) -------------------------------------------


def test_video_daily_limit_defaults_to_unlimited() -> None:
    profile = AccountRuntimeProfile.from_control_plane_payload("plus", {})

    assert profile.video_daily_limit is None


def test_video_daily_limit_parsed_from_control_plane() -> None:
    profile = AccountRuntimeProfile.from_control_plane_payload("plus", {
        "video_daily_limit": 3,
    })

    assert profile.video_daily_limit == 3


def test_video_daily_limit_rejects_zero_and_negative() -> None:
    """Same shape as story_scene_daily_limit: 0 is not a second spelling of
    "unlimited" (that's already video_generation_enabled=False's job), so an
    invalid 0/negative value falls back to the default (None)."""
    zero = AccountRuntimeProfile.from_control_plane_payload("plus", {
        "video_daily_limit": 0,
    })
    assert zero.video_daily_limit is None

    negative = AccountRuntimeProfile.from_control_plane_payload("plus", {
        "video_daily_limit": -1,
    })
    assert negative.video_daily_limit is None


def test_video_daily_limit_explicit_null_stays_unlimited() -> None:
    profile = AccountRuntimeProfile.from_control_plane_payload("plus", {
        "video_daily_limit": None,
    })

    assert profile.video_daily_limit is None


def test_video_daily_limit_missing_key_is_zero_impact_for_self_host() -> None:
    """self-host never sends this knob at all -- confirms the fail-open default
    stays unlimited with no payload change required."""
    assert DEFAULT_ACCOUNT_RUNTIME_PROFILE.video_daily_limit is None
