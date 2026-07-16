"""Yuralume Cloud federated auth strategy."""

from __future__ import annotations

from kokoro_link.application.exceptions import (
    DemoSessionUnavailable,
    InvalidCredentials,
    PermissionDenied,
    SetupNotAllowed,
)
from kokoro_link.application.services.jwt_service import JWTService
from kokoro_link.contracts.cloud_auth import (
    CloudAccountIdentity,
    CloudAuthRejected,
    CloudDemoSessionRejected,
    CloudAuthUpstreamError,
    CloudProfileSeed,
    CloudUserServicePort,
)
from kokoro_link.contracts.operator_profile import OperatorProfileRepositoryPort
from kokoro_link.domain.entities.operator_profile import (
    DEFAULT_PRIMARY_LANGUAGE,
    OperatorProfile,
    normalise_language_tag,
)
from kokoro_link.domain.value_objects.timezone import (
    DEFAULT_TIMEZONE_ID,
    normalise_timezone_id,
)


# Mirror the Yuralume User service role contract (UserRole: member | admin).
# Only "admin" maps to a hosted-core admin; do NOT add speculative synonyms —
# an unrecognised future role must default to non-admin to avoid silent
# privilege escalation.
_ADMIN_ROLES = {"admin"}


class CloudFederatedAuthStrategy:
    """Authenticate against Yuralume User service and project locally."""

    def __init__(
        self,
        *,
        user_service: CloudUserServicePort,
        repository: OperatorProfileRepositoryPort,
        jwt_service: JWTService,
        default_timezone_id: str = DEFAULT_TIMEZONE_ID,
        require_paid_tier: bool = False,
    ) -> None:
        self._user_service = user_service
        self._repo = repository
        self._jwt = jwt_service
        self._default_timezone_id = normalise_timezone_id(default_timezone_id)
        # Hosted deployments set this True so a free ``standard`` tenant can't
        # log in and consume hosted compute without an active membership. Off
        # by default for self-host / existing cloud (backward-compatible).
        self._require_paid_tier = require_paid_tier

    async def login(
        self,
        *,
        email: str,
        password: str,
        profile_seed: CloudProfileSeed | None = None,
    ) -> tuple[OperatorProfile, str]:
        if not email.strip() or not password:
            raise InvalidCredentials()
        try:
            identity = await self._user_service.login(
                email=email.strip().lower(),
                password=password,
            )
        except CloudAuthRejected as exc:
            raise InvalidCredentials() from exc
        except CloudAuthUpstreamError as exc:
            raise SetupNotAllowed(str(exc) or "cloud user service unavailable") from exc

        return await self._login_with_identity(identity, profile_seed)

    async def login_with_demo_session(
        self,
        *,
        provider: str,
        authorization_code: str,
        redirect_uri: str | None = None,
        code_verifier: str | None = None,
        source_ip: str | None = None,
        device_id: str | None = None,
        profile_seed: CloudProfileSeed | None = None,
    ) -> tuple[OperatorProfile, str]:
        if not provider.strip() or not authorization_code.strip():
            raise InvalidCredentials()
        try:
            identity = await self._user_service.create_demo_session(
                provider=provider.strip().lower(),
                authorization_code=authorization_code.strip(),
                redirect_uri=_normalise_optional_text(redirect_uri),
                code_verifier=_normalise_optional_text(code_verifier),
                source_ip=_normalise_optional_text(source_ip),
                device_id=_normalise_optional_text(device_id),
            )
        except CloudAuthRejected as exc:
            raise InvalidCredentials() from exc
        except CloudDemoSessionRejected as exc:
            raise DemoSessionUnavailable(
                status_code=exc.status_code,
                code=exc.code,
                message=exc.message,
                retryable=exc.retryable,
            ) from exc
        except CloudAuthUpstreamError as exc:
            raise SetupNotAllowed(str(exc) or "cloud user service unavailable") from exc

        return await self._login_with_identity(identity, profile_seed)

    async def login_with_cloud_play_code(
        self,
        *,
        code: str,
        profile_seed: CloudProfileSeed | None = None,
    ) -> tuple[OperatorProfile, str]:
        if not code.strip():
            raise InvalidCredentials()
        try:
            identity = await self._user_service.exchange_hosted_play_code(
                code=code.strip(),
            )
        except CloudAuthRejected as exc:
            raise InvalidCredentials() from exc
        except CloudAuthUpstreamError as exc:
            raise SetupNotAllowed(str(exc) or "cloud user service unavailable") from exc

        return await self._login_with_identity(identity, profile_seed)

    async def _login_with_identity(
        self,
        identity: CloudAccountIdentity,
        profile_seed: CloudProfileSeed | None = None,
    ) -> tuple[OperatorProfile, str]:
        if identity.status.strip().lower() != "active":
            raise PermissionDenied("cloud account is not active")
        self._authorize_tier(identity)
        operator = await self._project_operator(identity, profile_seed)
        return operator, self._jwt.encode(operator.id)

    def _authorize_tier(self, identity: CloudAccountIdentity) -> None:
        """Gate hosted compute on an active paid membership.

        ``demo`` is ALWAYS allowed — it has its own restricted runtime
        profile and TTL reaper flow. When ``require_paid_tier`` is off (the
        default for self-host / existing cloud) every tier is allowed. When
        on, a free ``standard`` tenant (or a blank/unknown-as-free tier) is
        rejected; any other non-empty (paid) tier passes.
        """
        tier = (identity.tenant_tier or "").strip().lower()
        if tier == "demo":
            return
        if not self._require_paid_tier:
            return
        if tier in {"standard", ""}:
            raise PermissionDenied("an active hosted membership is required")

    async def verify_token(self, token: str) -> OperatorProfile | None:
        user_id = self._jwt.user_id_from(token)
        if not user_id:
            return None
        operator = await self._repo.get(user_id)
        if operator is None or operator.auth_provider != "cloud":
            return None
        return operator

    def allows_local_setup(self) -> bool:
        return False

    def allows_user_crud(self) -> bool:
        return False

    async def _project_operator(
        self,
        identity: CloudAccountIdentity,
        profile_seed: CloudProfileSeed | None = None,
    ) -> OperatorProfile:
        account_id = _require_non_empty(identity.account_id, "account_id")
        tenant_id = _require_non_empty(identity.tenant_id, "tenant_id")
        operator_id = f"cloud:{account_id}"
        existing = await self._repo.get_by_cloud_account_id(account_id)
        if existing is None:
            existing = await self._repo.get(operator_id)

        seed = profile_seed or CloudProfileSeed()
        is_admin = identity.role.strip().lower() in _ADMIN_ROLES
        if existing is None:
            operator = OperatorProfile(
                id=operator_id,
                display_name=_display_name_for(identity),
                email=_normalise_email(identity.email),
                password_hash=None,
                is_admin=is_admin,
                primary_language=_primary_language_for(identity, seed),
                timezone_id=_timezone_for(identity, seed, self._default_timezone_id),
                country_code=seed.country_code,
                latitude=seed.latitude,
                longitude=seed.longitude,
                location_label=seed.location_label,
                cloud_account_id=account_id,
                cloud_tenant_id=tenant_id,
                cloud_tenant_tier=identity.tenant_tier,
                auth_provider="cloud",
            )
        else:
            # A player-locked display name (edited via the profile UI)
            # must survive OAuth re-login; only re-derive from identity
            # when the player hasn't taken ownership of the name.
            next_display_name = (
                existing.display_name
                if existing.display_name_locked
                else _display_name_for(identity, fallback=existing.display_name)
            )
            # cloud_tenant_tier is intentionally NOT re-stamped from the
            # identity here (H3): tier is push-authoritative via
            # ``set_cloud_tenant_tier_for_cloud_tenant`` (+ the first INSERT
            # below). Re-stamping the login-time identity tier would revert a
            # paid push that Cloud already committed and dropped its outbox
            # for, so it could never self-heal. Omitting it (update treats
            # ``None`` as leave-alone) preserves the stored/pushed tier.
            operator = existing.update(
                display_name=next_display_name,
                email=_normalise_email(identity.email) or existing.email,
                password_hash="",
                is_admin=is_admin,
                cloud_account_id=account_id,
                cloud_tenant_id=tenant_id,
                auth_provider="cloud",
            )
        await self._repo.save(operator)
        return operator


def _display_name_for(
    identity: CloudAccountIdentity, *, fallback: str | None = None,
) -> str:
    for candidate in (
        identity.display_name,
        identity.email.split("@", 1)[0] if identity.email else None,
        fallback,
        identity.account_id,
    ):
        if candidate and candidate.strip():
            return candidate.strip()
    return "Yuralume Player"


# Coarse country -> dominant content-language fallback, consulted only when
# the OAuth provider supplied no locale at all. Deliberately conservative:
# only countries with an unambiguous dominant *written* language are listed;
# everything else falls through to the project default so we never
# confidently mislabel a multi-language locale (CA, CH, BE, SG, IN, ...).
_COUNTRY_PRIMARY_LANGUAGE: dict[str, str] = {
    "TW": "zh-TW",
    "HK": "zh-TW",
    "MO": "zh-TW",
    "CN": "zh-CN",
    "JP": "ja",
    "KR": "ko",
    "US": "en",
    "GB": "en",
    "AU": "en",
    "NZ": "en",
    "IE": "en",
}


def _primary_language_for(
    identity: CloudAccountIdentity, seed: CloudProfileSeed,
) -> str:
    """Resolve the operator's pinned content language at creation time.

    Priority: the OAuth-provided locale (most accurate) → a conservative
    GeoIP country fallback → the project default. ``normalise_language_tag``
    raises on structurally broken tags, so a malformed OAuth locale degrades
    to the country/default path rather than poisoning the prompt-fact layer.
    """
    raw = identity.primary_language
    if raw and raw.strip():
        try:
            return normalise_language_tag(raw)
        except ValueError:
            pass
    country_language = _language_for_country(seed.country_code)
    if country_language is not None:
        return country_language
    return DEFAULT_PRIMARY_LANGUAGE


def _language_for_country(country_code: str | None) -> str | None:
    if not country_code:
        return None
    return _COUNTRY_PRIMARY_LANGUAGE.get(country_code.strip().upper())


def _timezone_for(
    identity: CloudAccountIdentity, seed: CloudProfileSeed, fallback: str,
) -> str:
    """Pin the operator's civil timezone at creation time.

    OAuth providers do not expose a timezone, so the GeoIP seed (ip-api's
    IANA ``timezone``) is the real source here; identity takes precedence in
    case the User service ever starts supplying one, and the deployment
    default is the last resort.
    """
    for candidate in (identity.timezone_id, seed.timezone_id):
        if candidate and candidate.strip():
            try:
                return normalise_timezone_id(candidate)
            except ValueError:
                continue
    return normalise_timezone_id(fallback)


def _normalise_email(raw: str | None) -> str | None:
    if raw is None:
        return None
    email = raw.strip().lower()
    return email or None


def _normalise_optional_text(raw: str | None) -> str | None:
    if raw is None:
        return None
    value = raw.strip()
    return value or None


def _require_non_empty(raw: str, field_name: str) -> str:
    value = (raw or "").strip()
    if not value:
        raise SetupNotAllowed(f"cloud user service response missing {field_name}")
    return value
