from __future__ import annotations

import pytest

from kokoro_link.application.exceptions import (
    DemoSessionUnavailable,
    InvalidCredentials,
    PermissionDenied,
)
from kokoro_link.application.services.cloud_auth_service import (
    CloudFederatedAuthStrategy,
)
from kokoro_link.application.services.jwt_service import JWTService
from kokoro_link.contracts.cloud_auth import (
    CloudAccountIdentity,
    CloudAuthRejected,
    CloudDemoSessionRejected,
    CloudProfileSeed,
)
from kokoro_link.infrastructure.repositories.in_memory_operator_profile import (
    InMemoryOperatorProfileRepository,
)


class _StubCloudUserService:
    def __init__(
        self,
        identity: CloudAccountIdentity | None = None,
        error: Exception | None = None,
    ) -> None:
        self.identity = identity
        self.error = error
        self.calls: list[tuple[str, str]] = []
        self.demo_calls: list[dict[str, str | None]] = []
        self.play_codes: list[str] = []

    async def login(self, *, email: str, password: str) -> CloudAccountIdentity:
        self.calls.append((email, password))
        if self.error is not None:
            raise self.error
        assert self.identity is not None
        return self.identity

    async def create_demo_session(
        self,
        *,
        provider: str,
        authorization_code: str,
        redirect_uri: str | None = None,
        code_verifier: str | None = None,
        source_ip: str | None = None,
        device_id: str | None = None,
    ) -> CloudAccountIdentity:
        self.demo_calls.append({
            "provider": provider,
            "authorization_code": authorization_code,
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
            "source_ip": source_ip,
            "device_id": device_id,
        })
        if self.error is not None:
            raise self.error
        assert self.identity is not None
        return self.identity

    async def exchange_hosted_play_code(
        self,
        *,
        code: str,
    ) -> CloudAccountIdentity:
        self.play_codes.append(code)
        if self.error is not None:
            raise self.error
        assert self.identity is not None
        return self.identity


def _strategy(
    user_service: _StubCloudUserService,
    *,
    require_paid_tier: bool = False,
) -> tuple[CloudFederatedAuthStrategy, InMemoryOperatorProfileRepository]:
    repo = InMemoryOperatorProfileRepository()
    return (
        CloudFederatedAuthStrategy(
            user_service=user_service,
            repository=repo,
            jwt_service=JWTService(
                secret="cloud-test-secret-at-least-32-bytes-long",
            ),
            default_timezone_id="Asia/Taipei",
            require_paid_tier=require_paid_tier,
        ),
        repo,
    )


@pytest.mark.asyncio
async def test_cloud_login_projects_operator_and_returns_core_jwt() -> None:
    strategy, repo = _strategy(_StubCloudUserService(CloudAccountIdentity(
        account_id="acct_123",
        tenant_id="tenant_abc",
        role="admin",
        status="active",
        tenant_tier="demo",
        email="PLAYER@EXAMPLE.COM",
        display_name=" Player One ",
        primary_language="en-us",
        timezone_id="America/Los_Angeles",
    )))

    operator, token = await strategy.login(
        email="PLAYER@EXAMPLE.COM",
        password="secret",
    )

    assert token
    assert operator.id == "cloud:acct_123"
    assert operator.email == "player@example.com"
    assert operator.display_name == "Player One"
    assert operator.is_admin is True
    assert operator.primary_language == "en-US"
    assert operator.timezone_id == "America/Los_Angeles"
    assert operator.cloud_account_id == "acct_123"
    assert operator.cloud_tenant_id == "tenant_abc"
    assert operator.cloud_tenant_tier == "demo"
    assert operator.auth_provider == "cloud"
    assert operator.password_hash is None
    assert await repo.get_by_cloud_account_id("acct_123") == operator
    assert await strategy.verify_token(token) == operator


@pytest.mark.asyncio
async def test_cloud_demo_session_projects_operator_and_returns_core_jwt() -> None:
    user_service = _StubCloudUserService(CloudAccountIdentity(
        account_id="demo_acct",
        tenant_id="demo_tenant",
        role="member",
        status="active",
        tenant_tier="demo",
        email="demo@example.com",
        display_name="Demo Player",
    ))
    strategy, repo = _strategy(user_service)

    operator, token = await strategy.login_with_demo_session(
        provider="discord",
        authorization_code="oauth-code",
        redirect_uri="https://app.example/demo/oauth/discord/callback",
        code_verifier="pkce",
        source_ip="198.51.100.44",
        device_id="device-1",
    )

    assert token
    assert operator.id == "cloud:demo_acct"
    assert operator.cloud_tenant_tier == "demo"
    assert await repo.get_by_cloud_account_id("demo_acct") == operator
    assert user_service.demo_calls == [{
        "provider": "discord",
        "authorization_code": "oauth-code",
        "redirect_uri": "https://app.example/demo/oauth/discord/callback",
        "code_verifier": "pkce",
        "source_ip": "198.51.100.44",
        "device_id": "device-1",
    }]


@pytest.mark.asyncio
async def test_cloud_play_code_projects_operator_and_returns_core_jwt() -> None:
    user_service = _StubCloudUserService(CloudAccountIdentity(
        account_id="acct_hosted",
        tenant_id="tenant_hosted",
        role="admin",
        status="active",
        tenant_tier="standard",
        email="player@example.com",
        display_name="Hosted Player",
    ))
    strategy, repo = _strategy(user_service)

    operator, token = await strategy.login_with_cloud_play_code(code="yhp_entry")

    assert token
    assert operator.id == "cloud:acct_hosted"
    assert operator.cloud_tenant_tier == "standard"
    assert operator.auth_provider == "cloud"
    assert operator.is_admin is True
    assert user_service.play_codes == ["yhp_entry"]
    assert await repo.get_by_cloud_account_id("acct_hosted") == operator
    assert await strategy.verify_token(token) == operator


@pytest.mark.asyncio
async def test_cloud_play_code_reuses_operator_on_reentry() -> None:
    user_service = _StubCloudUserService(CloudAccountIdentity(
        account_id="acct_hosted",
        tenant_id="tenant_hosted",
        role="member",
        status="active",
        email="player@example.com",
        display_name="Hosted Player",
    ))
    strategy, repo = _strategy(user_service)

    first, _ = await strategy.login_with_cloud_play_code(code="yhp_first")
    second, _ = await strategy.login_with_cloud_play_code(code="yhp_second")

    assert second.id == first.id
    assert len(await repo.list_all()) == 1


@pytest.mark.asyncio
async def test_cloud_play_code_rejected_maps_to_invalid_credentials() -> None:
    strategy, _ = _strategy(_StubCloudUserService(error=CloudAuthRejected()))

    with pytest.raises(InvalidCredentials):
        await strategy.login_with_cloud_play_code(code="yhp_gone")


@pytest.mark.asyncio
async def test_cloud_play_code_inactive_identity_denied() -> None:
    strategy, _ = _strategy(_StubCloudUserService(CloudAccountIdentity(
        account_id="acct_hosted",
        tenant_id="tenant_hosted",
        role="member",
        status="suspended",
    )))

    with pytest.raises(PermissionDenied):
        await strategy.login_with_cloud_play_code(code="yhp_entry")


def _demo_identity(**overrides: object) -> CloudAccountIdentity:
    base: dict[str, object] = {
        "account_id": "demo_acct",
        "tenant_id": "demo_tenant",
        "role": "member",
        "status": "active",
        "tenant_tier": "demo",
        "display_name": "Demo Player",
    }
    base.update(overrides)
    return CloudAccountIdentity(**base)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_demo_session_pins_timezone_and_location_from_geo_seed() -> None:
    # OAuth supplies the locale (language) but never a timezone; the GeoIP
    # seed is what pins the operator's civil timezone + editable location.
    user_service = _StubCloudUserService(_demo_identity(primary_language="zh-TW"))
    strategy, _ = _strategy(user_service)

    operator, _ = await strategy.login_with_demo_session(
        provider="discord",
        authorization_code="oauth-code",
        profile_seed=CloudProfileSeed(
            timezone_id="Asia/Tokyo",
            country_code="JP",
            latitude=35.68,
            longitude=139.69,
            location_label="Tokyo, JP",
        ),
    )

    assert operator.primary_language == "zh-TW"  # OAuth locale wins
    assert operator.timezone_id == "Asia/Tokyo"  # from GeoIP seed
    assert operator.country_code == "JP"
    assert operator.location_label == "Tokyo, JP"
    assert operator.latitude == pytest.approx(35.68)


@pytest.mark.asyncio
async def test_demo_session_language_falls_back_to_geo_country() -> None:
    # No OAuth locale at all -> conservative GeoIP country fallback applies.
    strategy, _ = _strategy(_StubCloudUserService(_demo_identity()))

    operator, _ = await strategy.login_with_demo_session(
        provider="discord",
        authorization_code="oauth-code",
        profile_seed=CloudProfileSeed(timezone_id="Asia/Taipei", country_code="JP"),
    )

    assert operator.primary_language == "ja"
    assert operator.timezone_id == "Asia/Taipei"


@pytest.mark.asyncio
async def test_demo_session_defaults_when_no_locale_and_ambiguous_country() -> None:
    # An ambiguous multi-language country (BE) is not mapped, so language
    # falls to the project default and timezone to the deployment default.
    strategy, _ = _strategy(_StubCloudUserService(_demo_identity()))

    operator, _ = await strategy.login_with_demo_session(
        provider="discord",
        authorization_code="oauth-code",
        profile_seed=CloudProfileSeed(country_code="BE"),
    )

    assert operator.primary_language == "zh-TW"  # project default
    assert operator.timezone_id == "Asia/Taipei"  # deployment default


@pytest.mark.asyncio
async def test_demo_session_ignores_seed_for_already_provisioned_operator() -> None:
    # timezone / language / location are pinned at creation; a later login
    # from a different geo must not rewrite them.
    user_service = _StubCloudUserService(_demo_identity(primary_language="zh-TW"))
    strategy, _ = _strategy(user_service)
    first, _ = await strategy.login_with_demo_session(
        provider="discord",
        authorization_code="code",
        profile_seed=CloudProfileSeed(timezone_id="Asia/Taipei", country_code="TW"),
    )

    second, _ = await strategy.login_with_demo_session(
        provider="discord",
        authorization_code="code",
        profile_seed=CloudProfileSeed(
            timezone_id="America/New_York", country_code="US",
        ),
    )

    assert second.id == first.id
    assert second.timezone_id == "Asia/Taipei"
    assert second.primary_language == "zh-TW"
    assert second.country_code == "TW"


@pytest.mark.asyncio
async def test_cloud_demo_session_preserves_structured_upstream_limit() -> None:
    strategy, _ = _strategy(_StubCloudUserService(error=CloudDemoSessionRejected(
        status_code=429,
        code="demo_rate_limited",
        message="demo session provisioning is rate limited for this source",
        retryable=True,
    )))

    with pytest.raises(DemoSessionUnavailable) as raised:
        await strategy.login_with_demo_session(
            provider="discord",
            authorization_code="oauth-code",
        )

    assert raised.value.status_code == 429
    assert raised.value.code == "demo_rate_limited"
    assert raised.value.retryable is True


@pytest.mark.asyncio
async def test_cloud_login_updates_existing_projection() -> None:
    user_service = _StubCloudUserService(CloudAccountIdentity(
        account_id="acct_123",
        tenant_id="tenant_one",
        role="member",
        status="active",
        email="player@example.com",
        display_name="Player",
    ))
    strategy, repo = _strategy(user_service)
    first, _ = await strategy.login(email="player@example.com", password="secret")

    user_service.identity = CloudAccountIdentity(
        account_id="acct_123",
        tenant_id="tenant_two",
        role="admin",
        status="active",
        tenant_tier="demo",
        email="player@example.com",
        display_name="Player Renamed",
    )
    second, _ = await strategy.login(email="player@example.com", password="secret")

    assert second.id == first.id
    assert second.display_name == "Player Renamed"
    assert second.cloud_tenant_id == "tenant_two"
    # Tier is push-authoritative (H3): the ordinary login re-projection no
    # longer stamps ``identity.tenant_tier`` onto an already-existing
    # operator, so the first-login value survives even though the identity
    # now reports "demo". Tier moves only via ``set_cloud_tenant_tier_for_
    # cloud_tenant`` / the first projection.
    assert second.cloud_tenant_tier == "standard"
    assert second.is_admin is True
    assert len(await repo.list_all()) == 1


@pytest.mark.asyncio
async def test_cloud_login_does_not_clobber_player_locked_display_name() -> None:
    user_service = _StubCloudUserService(CloudAccountIdentity(
        account_id="acct_123",
        tenant_id="tenant_one",
        role="member",
        status="active",
        email="player@example.com",
        display_name="OAuthName",
    ))
    strategy, repo = _strategy(user_service)
    first, _ = await strategy.login(email="player@example.com", password="secret")

    # Player edits their display name via the profile UI (sets the lock).
    edited = (await repo.get_by_cloud_account_id("acct_123")).update(
        display_name="阿丹", display_name_locked=True,
    )
    await repo.save(edited)

    # Provider sends a different display name on the next login.
    user_service.identity = CloudAccountIdentity(
        account_id="acct_123",
        tenant_id="tenant_one",
        role="member",
        status="active",
        email="player@example.com",
        display_name="OAuthRenamed",
    )
    second, _ = await strategy.login(email="player@example.com", password="secret")

    assert second.id == first.id
    assert second.display_name == "阿丹"  # player edit survives OAuth re-login
    assert second.display_name_locked is True


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["member", "owner", "operator", "viewer", ""])
async def test_cloud_login_only_admin_role_grants_admin(role: str) -> None:
    # Mirror the User service contract (UserRole: member | admin). Any role
    # other than the documented "admin" must project a non-admin operator so
    # an unrecognised future role can't silently escalate privilege.
    strategy, _ = _strategy(_StubCloudUserService(CloudAccountIdentity(
        account_id="acct_role",
        tenant_id="tenant_role",
        role=role,
        status="active",
        email="player@example.com",
        display_name="Player",
    )))

    operator, _ = await strategy.login(
        email="player@example.com", password="secret",
    )

    assert operator.is_admin is False


@pytest.mark.asyncio
async def test_cloud_login_rejects_bad_credentials() -> None:
    strategy, _ = _strategy(_StubCloudUserService(error=CloudAuthRejected()))

    with pytest.raises(InvalidCredentials):
        await strategy.login(email="ghost@example.com", password="bad")


@pytest.mark.asyncio
async def test_cloud_login_rejects_suspended_account() -> None:
    strategy, _ = _strategy(_StubCloudUserService(CloudAccountIdentity(
        account_id="acct_123",
        tenant_id="tenant_abc",
        role="member",
        status="suspended",
    )))

    with pytest.raises(PermissionDenied):
        await strategy.login(email="player@example.com", password="secret")


# ---------------- H1: paid-tier authorization gate --------------------


def _identity(tier: str, *, status: str = "active") -> CloudAccountIdentity:
    return CloudAccountIdentity(
        account_id="acct_tier",
        tenant_id="tenant_tier",
        role="member",
        status=status,
        tenant_tier=tier,
        email="player@example.com",
        display_name="Player",
    )


@pytest.mark.asyncio
async def test_require_paid_tier_blocks_standard_password_login() -> None:
    strategy, repo = _strategy(
        _StubCloudUserService(_identity("standard")), require_paid_tier=True,
    )

    with pytest.raises(PermissionDenied):
        await strategy.login(email="player@example.com", password="secret")
    # Rejected before any local projection is written.
    assert await repo.get_by_cloud_account_id("acct_tier") is None


@pytest.mark.asyncio
async def test_require_paid_tier_blocks_standard_demo_session_login() -> None:
    strategy, _ = _strategy(
        _StubCloudUserService(_identity("standard")), require_paid_tier=True,
    )

    with pytest.raises(PermissionDenied):
        await strategy.login_with_demo_session(
            provider="discord", authorization_code="oauth-code",
        )


@pytest.mark.asyncio
async def test_require_paid_tier_blocks_standard_play_code_login() -> None:
    strategy, _ = _strategy(
        _StubCloudUserService(_identity("standard")), require_paid_tier=True,
    )

    with pytest.raises(PermissionDenied):
        await strategy.login_with_cloud_play_code(code="yhp_entry")


@pytest.mark.asyncio
async def test_require_paid_tier_blocks_blank_tier() -> None:
    strategy, _ = _strategy(
        _StubCloudUserService(_identity("")), require_paid_tier=True,
    )

    with pytest.raises(PermissionDenied):
        await strategy.login(email="player@example.com", password="secret")


@pytest.mark.asyncio
async def test_require_paid_tier_allows_demo_identity() -> None:
    # Demo has its own restricted profile + reaper flow, so it is always
    # allowed regardless of the paid-tier gate.
    strategy, _ = _strategy(
        _StubCloudUserService(_identity("demo")), require_paid_tier=True,
    )

    operator, token = await strategy.login(
        email="player@example.com", password="secret",
    )

    assert token
    assert operator.cloud_tenant_tier == "demo"


@pytest.mark.asyncio
async def test_require_paid_tier_allows_paid_identity() -> None:
    strategy, _ = _strategy(
        _StubCloudUserService(_identity("plus")), require_paid_tier=True,
    )

    operator, token = await strategy.login(
        email="player@example.com", password="secret",
    )

    assert token
    assert operator.cloud_tenant_tier == "plus"


@pytest.mark.asyncio
async def test_standard_tier_allowed_when_gate_disabled() -> None:
    # Regression: default (self-host / existing cloud) keeps today's behavior
    # where a standard tenant logs in fine.
    strategy, _ = _strategy(
        _StubCloudUserService(_identity("standard")), require_paid_tier=False,
    )

    operator, token = await strategy.login(
        email="player@example.com", password="secret",
    )

    assert token
    assert operator.cloud_tenant_tier == "standard"


# ---------------- H3: tier push is authoritative ----------------------


@pytest.mark.asyncio
async def test_relogin_preserves_pushed_tier_over_identity_tier() -> None:
    # First login stamps the identity tier; a later authoritative push moves
    # it; a subsequent ordinary login whose identity still reports the OLD
    # tier must NOT revert the pushed tier.
    user_service = _StubCloudUserService(CloudAccountIdentity(
        account_id="acct_push",
        tenant_id="tenant_push",
        role="member",
        status="active",
        tenant_tier="standard",
        email="player@example.com",
        display_name="Player",
    ))
    strategy, repo = _strategy(user_service)

    first, _ = await strategy.login(email="player@example.com", password="secret")
    assert first.cloud_tenant_tier == "standard"  # first login stamps

    # Cloud pushes the tenant to a paid tier via the dedicated path.
    pushed = await repo.set_cloud_tenant_tier_for_cloud_tenant(
        "tenant_push", "plus",
    )
    assert pushed == 1

    # Identity still reports the stale "standard" tier on the next login.
    second, _ = await strategy.login(email="player@example.com", password="secret")

    assert second.id == first.id
    assert second.cloud_tenant_tier == "plus"  # pushed tier survives re-login
