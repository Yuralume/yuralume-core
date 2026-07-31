"""AP4 — the player-facing overage switches on the operator settings surface.

Cloud-only by construction: ``create_app`` mounts ``overage_router`` only in
cloud mode (self-host's route inventory is unchanged — see
``test_cloud_only_route_mounting``), and the handlers 404 as well so the two
endpoints stay safe wherever they are mounted.

The write side is not a preference write: turning a switch **on** is a priced
agreement and answers a structured 400 when it cannot be given, while turning
one **off** always succeeds.
"""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from kokoro_link.api.dependencies import get_current_user_id
from kokoro_link.api.routes.operator import overage_router
from kokoro_link.application.services.cloud_pricing_service import (
    PricingSnapshot,
)
from kokoro_link.application.services.quota_overage_service import (
    QuotaOverageService,
)
from kokoro_link.contracts.cloud_action_pricing import (
    ActionPrice,
    PublicPricing,
    TierPricing,
)
from kokoro_link.domain.value_objects.account_runtime_profile import (
    BILLING_SHAPE_ACTION_FIXED,
    AccountRuntimeProfile,
)
from kokoro_link.infrastructure.repositories.in_memory_account_runtime_usage import (
    InMemoryAccountRuntimeUsageRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_preferences import (
    InMemoryPreferencesRepository,
)

OPERATOR = "cloud:acct-1"


class _StaticProfiles:
    def __init__(self, profile: AccountRuntimeProfile) -> None:
        self._profile = profile

    async def resolve_for_operator(self, operator_id: str):
        return self._profile


class _InertBilling:
    async def begin(self, action_key, *, operator_id, action_kind):
        return None

    async def settle(self, handle) -> None:
        return None

    async def release(self, handle) -> None:
        return None


class _StaticPricing:
    def __init__(self, snapshot: PricingSnapshot | None) -> None:
        self._snapshot = snapshot

    async def get_pricing(self, *, refresh: bool = False):
        return self._snapshot


def _pricing(*, chat_image_cr: float | None = 8.0) -> _StaticPricing:
    actions = tuple(
        ActionPrice(action_key=key, unit=unit, price_cr=price, overage=True)
        for key, unit, price in (
            ("chat_image_overage", "per_image", chat_image_cr),
            ("feed_post_overage", "per_post", 6.0),
        )
        if price is not None
    )
    return _StaticPricing(PricingSnapshot(
        pricing=PublicPricing(tiers=(
            TierPricing(
                tier_name="internal_test",
                billing_shape=BILLING_SHAPE_ACTION_FIXED,
                actions=actions,
            ),
        )),
        stale=False,
    ))


def _client(
    *,
    cloud_active: bool = True,
    service: QuotaOverageService | None = None,
    profile: AccountRuntimeProfile | None = None,
) -> tuple[TestClient, QuotaOverageService | None]:
    if service is None and profile is not None:
        service = _default_service(profile=profile)
    app = FastAPI()
    app.include_router(overage_router, prefix="/api/v1")
    app.state.container = SimpleNamespace(
        app_settings=SimpleNamespace(cloud=SimpleNamespace(active=cloud_active)),
        operator_profile_service=None,
        quota_overage_service=service,
    )
    app.dependency_overrides[get_current_user_id] = lambda: OPERATOR
    return TestClient(app), service


def _default_service(
    *,
    profile: AccountRuntimeProfile | None = None,
    pricing: _StaticPricing | None = None,
) -> QuotaOverageService:
    return QuotaOverageService(
        preferences=InMemoryPreferencesRepository(),
        profile_resolver=_StaticProfiles(
            profile or AccountRuntimeProfile(
                name="internal_test",
                billing_shape=BILLING_SHAPE_ACTION_FIXED,
                overage_enabled=True,
                daily_overage_limit=5,
            ),
        ),
        usage_repository=InMemoryAccountRuntimeUsageRepository(),
        action_billing=_InertBilling(),
        pricing=pricing if pricing is not None else _pricing(),
    )


def test_settings_start_switched_off() -> None:
    client, _ = _client(service=_default_service())

    response = client.get("/api/v1/operator/overage-settings")

    assert response.status_code == 200
    assert response.json() == {
        "chat_image_enabled": False,
        "feed_post_enabled": False,
        "tier_overage_enabled": True,
        "daily_limit": 5,
        "chat_image_price_cr": 8.0,
        "feed_post_price_cr": 6.0,
    }


def test_a_switch_can_be_turned_on_and_read_back() -> None:
    client, _ = _client(service=_default_service())

    written = client.put(
        "/api/v1/operator/overage-settings",
        json={"feed_post_enabled": True},
    )

    assert written.status_code == 200
    assert written.json()["feed_post_enabled"] is True
    assert written.json()["chat_image_enabled"] is False
    read_back = client.get("/api/v1/operator/overage-settings")
    assert read_back.json()["feed_post_enabled"] is True


def test_an_omitted_switch_is_left_alone() -> None:
    client, _ = _client(service=_default_service())
    client.put(
        "/api/v1/operator/overage-settings",
        json={"chat_image_enabled": True, "feed_post_enabled": True},
    )

    response = client.put(
        "/api/v1/operator/overage-settings",
        json={"feed_post_enabled": False},
    )

    assert response.json() == {
        "chat_image_enabled": True,
        "feed_post_enabled": False,
        "tier_overage_enabled": True,
        "daily_limit": 5,
        "chat_image_price_cr": 8.0,
        "feed_post_price_cr": 6.0,
    }


def test_a_tier_that_never_opened_overage_is_reported_as_such() -> None:
    client, _ = _client(
        profile=AccountRuntimeProfile(name="free", overage_enabled=False),
    )

    response = client.get("/api/v1/operator/overage-settings")

    assert response.status_code == 200
    assert response.json()["tier_overage_enabled"] is False


def test_the_switches_are_hidden_in_self_host_mode() -> None:
    client, _ = _client(cloud_active=False, service=_default_service())

    assert client.get("/api/v1/operator/overage-settings").status_code == 404
    assert client.put(
        "/api/v1/operator/overage-settings",
        json={"chat_image_enabled": True},
    ).status_code == 404


def test_an_unwired_service_degrades_to_503() -> None:
    client, _ = _client(service=None)

    assert client.get("/api/v1/operator/overage-settings").status_code == 503


def test_arming_a_switch_with_no_readable_price_is_a_structured_400() -> None:
    """C6: the SPA must be able to say *why*, not just spring the toggle back."""
    client, _ = _client(
        service=_default_service(pricing=_StaticPricing(None)),
    )

    response = client.put(
        "/api/v1/operator/overage-settings",
        json={"chat_image_enabled": True},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == {
        "code": "overage_consent_rejected",
        "reason": "price_unavailable",
        "item": "chat_image",
        "message": (
            "the current price of chat_image_overage cannot be read, "
            "so it cannot be agreed to"
        ),
    }
    read_back = client.get("/api/v1/operator/overage-settings").json()
    assert read_back["chat_image_enabled"] is False


def test_a_plan_that_does_not_sell_overage_refuses_the_arm() -> None:
    client, _ = _client(
        profile=AccountRuntimeProfile(
            name="internal_test",
            billing_shape=BILLING_SHAPE_ACTION_FIXED,
            overage_enabled=False,
        ),
    )

    response = client.put(
        "/api/v1/operator/overage-settings",
        json={"feed_post_enabled": True},
    )

    assert response.status_code == 400
    assert response.json()["detail"]["reason"] == "tier_closed"


def test_switching_off_is_accepted_even_when_arming_would_not_be() -> None:
    client, _ = _client(
        service=_default_service(pricing=_StaticPricing(None)),
    )

    response = client.put(
        "/api/v1/operator/overage-settings",
        json={"chat_image_enabled": False},
    )

    assert response.status_code == 200
    assert response.json()["chat_image_enabled"] is False
