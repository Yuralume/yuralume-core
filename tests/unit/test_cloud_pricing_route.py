"""AP3 — ``GET /api/v1/cloud/pricing`` player-facing price-list proxy.

Cloud-mode only in the strong sense: ``create_app`` does not *mount* this
router (nor the AP4 overage one) outside cloud mode, so a self-host install's
route inventory — OpenAPI included — is exactly what it was before AP3/AP4
existed. The in-handler 404 is kept as a second line and is what the rest of
this module exercises. An outage with nothing cached answers 503 rather than an
empty (i.e. free-looking) list.
"""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from kokoro_link.api.app import create_app
from kokoro_link.api.routes.cloud_pricing import router as cloud_pricing_router
from kokoro_link.bootstrap.settings import AppSettings, CloudSettings
from kokoro_link.application.services.cloud_pricing_service import (
    PricingSnapshot,
)
from kokoro_link.contracts.cloud_action_pricing import (
    ActionPrice,
    PublicPricing,
    TierPricing,
)


class _StubPricingService:
    def __init__(self, snapshot: PricingSnapshot | None) -> None:
        self._snapshot = snapshot
        self.calls: list[bool] = []

    async def get_pricing(self, *, refresh: bool = False):
        self.calls.append(refresh)
        return self._snapshot


def _snapshot(*, stale: bool = False) -> PricingSnapshot:
    return PricingSnapshot(
        pricing=PublicPricing(
            tiers=(
                TierPricing(
                    tier_name="standard",
                    billing_shape="action_fixed",
                    actions=(
                        ActionPrice(
                            action_key="chat",
                            unit="per_message",
                            price_cr=2.5,
                        ),
                    ),
                ),
            ),
        ),
        stale=stale,
    )


def _client(
    *, cloud_active: bool = True, service: _StubPricingService | None = None,
) -> TestClient:
    app = FastAPI()
    app.include_router(cloud_pricing_router, prefix="/api/v1")
    app.state.container = SimpleNamespace(
        app_settings=SimpleNamespace(cloud=SimpleNamespace(active=cloud_active)),
        cloud_pricing_service=service,
    )
    return TestClient(app)


def test_returns_the_published_price_list() -> None:
    client = _client(service=_StubPricingService(_snapshot()))

    response = client.get("/api/v1/cloud/pricing")

    assert response.status_code == 200
    assert response.json() == {
        "stale": False,
        "tiers": [
            {
                "tier_name": "standard",
                "billing_shape": "action_fixed",
                "actions": [
                    {
                        "action_key": "chat",
                        "unit": "per_message",
                        "price_cr": 2.5,
                        "overage": False,
                    },
                ],
            },
        ],
    }


def test_refresh_is_forwarded_to_the_cache() -> None:
    service = _StubPricingService(_snapshot())
    client = _client(service=service)

    client.get("/api/v1/cloud/pricing", params={"refresh": "true"})

    assert service.calls == [True]


def test_stale_is_reported_rather_than_hidden() -> None:
    client = _client(service=_StubPricingService(_snapshot(stale=True)))

    assert client.get("/api/v1/cloud/pricing").json()["stale"] is True


def test_self_host_never_sees_the_surface() -> None:
    client = _client(cloud_active=False, service=_StubPricingService(_snapshot()))

    assert client.get("/api/v1/cloud/pricing").status_code == 404


def test_nothing_known_answers_503_not_an_empty_list() -> None:
    client = _client(service=_StubPricingService(None))

    assert client.get("/api/v1/cloud/pricing").status_code == 503


def test_unwired_service_degrades_the_same_way() -> None:
    client = _client(service=None)

    assert client.get("/api/v1/cloud/pricing").status_code == 503


# -- mounting (H2): self-host's route inventory is untouched -----------

_HOSTED_ONLY_PATHS = (
    "/api/v1/cloud/pricing",
    "/api/v1/operator/overage-settings",
)


def _paths(settings: AppSettings) -> set[str]:
    return {route.path for route in create_app(settings).routes}


def test_self_host_does_not_mount_the_hosted_only_routes() -> None:
    """A route that exists only to 404 still shows up in the API inventory."""
    paths = _paths(AppSettings(database_url=""))

    assert not paths & set(_HOSTED_ONLY_PATHS)
    # ...while the settings surface these hang off is untouched.
    assert "/api/v1/operator/profile" in paths


def test_cloud_mode_mounts_them() -> None:
    paths = _paths(AppSettings(
        database_url="",
        cloud=CloudSettings(
            enabled=True,
            user_service_url="https://user.test",
            gateway_url="https://gateway.test",
            deployment_token="deployment-token",
        ),
    ))

    assert set(_HOSTED_ONLY_PATHS) <= paths
