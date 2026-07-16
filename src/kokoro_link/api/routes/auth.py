"""Auth router — setup / login / me / users CRUD.

Every endpoint here is exempt from the global bearer dependency the
other routers carry (batch 3b): public ones (config / setup-status /
setup / login) must be reachable without a token, and authenticated
ones (me / users) have a per-handler ``Depends(...)`` instead of the
blanket include_router dependency.

Front-end startup flow:
1. ``GET /auth/config`` — read ``auth_enabled`` + ``needs_setup``.
2. If ``auth_enabled=false`` → stash that and skip the whole login UI.
3. If ``needs_setup`` → route to ``/setup`` page.
4. Else → route to ``/login``.
"""

from __future__ import annotations

import logging
from ipaddress import ip_address

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, EmailStr, Field, field_validator

from kokoro_link.api.contracts.build_info import (
    BuildInfoResponse,
    build_info_response,
)
from kokoro_link.api.dependencies import (
    get_container,
    get_current_user,
    is_cloud_mode,
    require_admin,
    require_self_host_mode,
)
from kokoro_link.application.exceptions import (
    AuthError,
    DemoSessionUnavailable,
    InvalidCredentials,
    PermissionDenied,
    SetupAlreadyComplete,
    SetupNotAllowed,
    UserAlreadyExists,
    UserNotFound,
)
from kokoro_link.bootstrap.container import ServiceContainer
from kokoro_link.contracts.cloud_auth import CloudProfileSeed
from kokoro_link.contracts.geo_location import GeoLocation
from kokoro_link.domain.entities.operator_profile import (
    OperatorProfile,
    normalise_language_tag,
)
from kokoro_link.domain.value_objects.timezone import normalise_timezone_id
from kokoro_link.infrastructure.build_info import get_build_info

router = APIRouter(prefix="/auth", tags=["auth"])

_LOGGER = logging.getLogger(__name__)

_IP_HEADER_CANDIDATE_NAMES = (
    "forwarded",
    "x-forwarded-for",
    "x-real-ip",
    "x-client-ip",
    "x-cluster-client-ip",
    "x-forwarded",
    "x-original-forwarded-for",
    "cf-connecting-ip",
    "cf-connecting-ipv6",
    "cf-pseudo-ipv4",
    "true-client-ip",
    "fastly-client-ip",
    "fly-client-ip",
    "x-appengine-user-ip",
    "x-azure-clientip",
    "x-vercel-forwarded-for",
    "x-nf-client-connection-ip",
)


# ----------------------------------------------------------------------
# DTOs
# ----------------------------------------------------------------------


class AuthConfigResponse(BaseModel):
    """Front-end startup probe."""

    auth_enabled: bool
    needs_setup: bool
    mode: str = "self_host"
    debug_ui_enabled: bool = False
    build_info: BuildInfoResponse
    """Mirror of ``AppSettings.debug_ui_enabled`` (env
    ``KOKORO_DEBUG_UI_ENABLED``). Lets the SPA decide whether to
    render developer-facing admin panels (observability, experiments,
    pending follow-ups, subsystem health metrics, persona drift / pattern
    timelines) on startup. Backend admin APIs are unaffected — flag
    only controls UI rendering."""


class DemoOAuthProviderConfigResponse(BaseModel):
    client_id: str


class DemoOAuthConfigResponse(BaseModel):
    """Public demo OAuth client ids, fetched by the SPA at runtime (plan Phase 5.1).

    Removes the Vite build-time client-id bake so changing a client id no longer
    needs a fresh SPA image. Values are public client ids only — never secrets.
    """

    providers: dict[str, DemoOAuthProviderConfigResponse]


class SetupStatusResponse(BaseModel):
    needs_setup: bool


class SetupRequest(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=1)
    primary_language: str = Field(
        default="zh-TW",
        min_length=2,
        max_length=16,
        description=(
            "BCP 47 tag for the operator's content language (LLM "
            "output, memory, persona). Immutable after setup — see "
            "docs/FRONTEND_I18N_PLAN.md §使用者主要語言. Defaults to "
            "'zh-TW' for backward-compatible clients that haven't "
            "rolled out the i18n picker yet."
        ),
    )
    timezone_id: str | None = Field(
        default=None,
        max_length=64,
        description=(
            "IANA timezone id for user-visible civil dates/times. "
            "Captured at setup and immutable after registration. "
            "When omitted, the deployment bootstrap default is used."
        ),
    )
    country_code: str | None = Field(default=None, min_length=2, max_length=2)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    location_label: str | None = Field(default=None, max_length=128)

    @field_validator("primary_language")
    @classmethod
    def _normalise_language(cls, v: str) -> str:
        # Structural validation lives in the domain helper so the entity
        # invariant and the API contract can't drift apart. Pydantic
        # re-raises the ValueError as 422 — the right shape for a
        # malformed request body (not 401 auth-failure).
        return normalise_language_tag(v)

    @field_validator("timezone_id")
    @classmethod
    def _normalise_timezone(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return normalise_timezone_id(v)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=1)


class DemoSessionLoginRequest(BaseModel):
    provider: str = Field(..., min_length=1, max_length=32)
    authorization_code: str = Field(..., min_length=1)
    redirect_uri: str | None = Field(default=None, max_length=512)
    code_verifier: str | None = Field(default=None, max_length=256)


class CloudPlaySessionRequest(BaseModel):
    """One-time hosted-play code issued by the customer portal (plan H0)."""

    code: str = Field(..., min_length=1, max_length=256)


class UserResponse(BaseModel):
    """Public view of an operator profile — never includes the
    password hash. ``email`` may be ``None`` on the pre-setup default
    user."""

    id: str
    display_name: str
    display_name_is_placeholder: bool = False
    """True when ``display_name`` is still the seeded ``操作者`` sentinel
    (operator skipped naming). The frontend maps this to a localized
    placeholder label (en ``Operator`` / ja ``オペレーター``) at the display
    boundary — the stored value + prompt sentinel are never mutated
    (checksum + address_resolver red line)."""
    email: str | None
    is_admin: bool
    primary_language: str
    timezone_id: str
    country_code: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    location_label: str | None = None

    @classmethod
    def from_domain(cls, user: OperatorProfile) -> "UserResponse":
        return cls(
            id=user.id,
            display_name=user.display_name,
            display_name_is_placeholder=not user.has_real_name(),
            email=user.email,
            is_admin=user.is_admin,
            primary_language=user.primary_language,
            timezone_id=user.timezone_id,
            country_code=user.country_code,
            latitude=user.latitude,
            longitude=user.longitude,
            location_label=user.location_label,
        )


class AuthTokenResponse(BaseModel):
    """``setup`` + ``login`` payload: profile + freshly issued token."""

    user: UserResponse
    token: str


class CreateUserRequest(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=1)
    display_name: str = Field(..., min_length=1)
    is_admin: bool = False
    primary_language: str = Field(
        default="zh-TW",
        min_length=2,
        max_length=16,
        description=(
            "BCP 47 tag for the new operator's content language. "
            "Immutable after creation."
        ),
    )
    timezone_id: str | None = Field(
        default=None,
        max_length=64,
        description=(
            "IANA timezone id for user-visible civil dates/times. "
            "Captured when the user is created and immutable afterwards. "
            "When omitted, the deployment bootstrap default is used."
        ),
    )
    country_code: str | None = Field(default=None, min_length=2, max_length=2)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    location_label: str | None = Field(default=None, max_length=128)

    @field_validator("primary_language")
    @classmethod
    def _normalise_language(cls, v: str) -> str:
        return normalise_language_tag(v)

    @field_validator("timezone_id")
    @classmethod
    def _normalise_timezone(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return normalise_timezone_id(v)


class ChangePasswordRequest(BaseModel):
    new_password: str = Field(..., min_length=1)


class ChangeOwnPasswordRequest(BaseModel):
    current_password: str = Field(..., min_length=1)
    new_password: str = Field(..., min_length=1)


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------


def _require_auth_service(container: ServiceContainer):
    svc = container.auth_service
    if svc is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="auth service not configured",
        )
    return svc


def _require_auth_strategy(container: ServiceContainer):
    strategy = getattr(container, "auth_strategy", None)
    if strategy is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="auth service not configured",
        )
    return strategy


def _translate_auth_error(exc: AuthError) -> HTTPException:
    """Map application-layer auth errors to HTTP status codes.

    Kept in one place so a future error class gets a deliberate code
    rather than defaulting to 500."""
    if isinstance(exc, InvalidCredentials):
        return HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc),
        )
    if isinstance(exc, PermissionDenied):
        return HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=str(exc),
        )
    if isinstance(exc, SetupAlreadyComplete):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc),
        )
    if isinstance(exc, SetupNotAllowed):
        return HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc),
        )
    if isinstance(exc, DemoSessionUnavailable):
        return HTTPException(
            status_code=exc.status_code,
            detail={
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "retryable": exc.retryable,
                },
            },
        )
    if isinstance(exc, UserAlreadyExists):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc),
        )
    if isinstance(exc, UserNotFound):
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc),
        )
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc),
    )


def _is_auth_enabled(container: ServiceContainer) -> bool:
    app_settings = getattr(container, "app_settings", None)
    if app_settings is None:
        return False
    cloud = getattr(app_settings, "cloud", None)
    if bool(getattr(cloud, "active", False)):
        return True
    auth = getattr(app_settings, "auth", None)
    if auth is None:
        return False
    return bool(getattr(auth, "enabled", False))


def _explicit_location_fields(
    payload: BaseModel,
) -> tuple[str | None, float | None, float | None, str | None] | None:
    location_fields = {
        "country_code", "latitude", "longitude", "location_label",
    }
    if not (payload.model_fields_set & location_fields):
        return None
    return (
        getattr(payload, "country_code", None),
        getattr(payload, "latitude", None),
        getattr(payload, "longitude", None),
        getattr(payload, "location_label", None),
    )


def _client_ip_from_request(request: Request) -> str | None:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded.strip():
        return forwarded.split(",", 1)[0].strip() or None
    real_ip = request.headers.get("x-real-ip", "")
    if real_ip.strip():
        return real_ip.strip()
    if request.client is None:
        return None
    return request.client.host


def _trusted_edge_client_ip(request: Request) -> str | None:
    """Return an edge-overwritten literal for security-sensitive forwarding.

    Hosted nginx replaces X-Real-IP with its socket peer. X-Forwarded-For is
    deliberately ignored here because append-style proxy configuration can
    preserve a browser-supplied first element. A direct literal socket peer is
    an acceptable fallback when no reverse proxy is present.
    """
    candidates = [request.headers.get("x-real-ip", "")]
    if request.client is not None:
        candidates.append(request.client.host)
    for raw in candidates:
        value = raw.strip()
        if not value:
            continue
        try:
            return str(ip_address(value))
        except ValueError:
            continue
    return None


def _ip_header_candidates_from_request(request: Request) -> dict[str, str]:
    candidates: dict[str, str] = {}
    for name in _IP_HEADER_CANDIDATE_NAMES:
        value = request.headers.get(name, "").strip()
        if value:
            candidates[name] = value
    if request.client is not None:
        candidates["request.client.host"] = request.client.host
    return candidates


async def _location_from_request(
    *,
    request: Request,
    container: ServiceContainer,
) -> GeoLocation | None:
    provider = getattr(container, "geo_location_provider", None)
    if provider is None:
        return None
    client_ip = _client_ip_from_request(request)
    if not client_ip:
        return None
    try:
        return await provider.locate(client_ip)
    except Exception as exc:  # noqa: BLE001 - setup must not fail on GeoIP
        _LOGGER.info("GeoIP provider failed for %s: %s", client_ip, exc)
        return None


def _location_fields_from_geo(
    location: GeoLocation | None,
) -> tuple[str | None, float | None, float | None, str | None]:
    if location is None:
        return None, None, None, None
    return (
        location.country_code,
        location.latitude,
        location.longitude,
        location.label,
    )


def _profile_seed_from_location(location: GeoLocation | None) -> CloudProfileSeed:
    """Project a GeoIP result into the creation-time seed the cloud auth
    strategy consumes. Timezone and country ride along so the strategy can
    pin the operator's immutable timezone (and language fallback) the first
    time a cloud/demo account is provisioned; an absent location yields an
    empty seed and the strategy falls back to identity / deployment values.
    """
    if location is None:
        return CloudProfileSeed()
    return CloudProfileSeed(
        timezone_id=location.timezone_id,
        country_code=location.country_code,
        latitude=location.latitude,
        longitude=location.longitude,
        location_label=location.label,
    )


def _geo_location_log_fields(location: GeoLocation | None) -> dict[str, object] | None:
    if location is None:
        return None
    return {
        "country_code": location.country_code,
        "latitude": location.latitude,
        "longitude": location.longitude,
        "label": location.label,
    }


def _has_location(user: OperatorProfile) -> bool:
    return any((
        user.country_code is not None,
        user.latitude is not None,
        user.longitude is not None,
        user.location_label is not None,
    ))


async def _seed_missing_location_from_login(
    *,
    user: OperatorProfile,
    request: Request,
    container: ServiceContainer,
) -> OperatorProfile:
    has_existing_location = _has_location(user)
    selected_client_ip = _client_ip_from_request(request)
    _LOGGER.info(
        "Login IP diagnostics: user_id=%s has_existing_location=%s "
        "selected_client_ip=%s candidates=%s",
        user.id,
        has_existing_location,
        selected_client_ip,
        _ip_header_candidates_from_request(request),
    )
    if has_existing_location:
        return user
    repository = getattr(container, "operator_profile_repository", None)
    if repository is None:
        return user
    location = await _location_from_request(request=request, container=container)
    _LOGGER.info(
        "Login GeoIP result: user_id=%s selected_client_ip=%s result=%s",
        user.id,
        selected_client_ip,
        _geo_location_log_fields(location),
    )
    if location is None:
        return user
    country_code, latitude, longitude, location_label = (
        _location_fields_from_geo(location)
    )
    updated = user.update(
        country_code=country_code,
        latitude=latitude,
        longitude=longitude,
        location_label=location_label,
    )
    _LOGGER.info(
        "Login GeoIP seed fields: user_id=%s country_code=%s latitude=%s "
        "longitude=%s location_label=%s",
        user.id,
        updated.country_code,
        updated.latitude,
        updated.longitude,
        updated.location_label,
    )
    try:
        await repository.save(updated)
    except Exception as exc:  # noqa: BLE001 - login must not fail on GeoIP seed
        _LOGGER.info("Failed to persist login GeoIP seed for %s: %s", user.id, exc)
        return user
    return updated


# ----------------------------------------------------------------------
# Public probes
# ----------------------------------------------------------------------


@router.get("/config", response_model=AuthConfigResponse)
async def get_auth_config(
    container: ServiceContainer = Depends(get_container),
) -> AuthConfigResponse:
    """Front-end startup probe — auth toggle, setup status, debug-UI flag.

    The SPA bootstraps off this endpoint and stashes the response in
    its auth store so router guards and admin panels can react to all
    three flags without a second round-trip."""
    needs_setup = False
    if container.auth_service is not None:
        needs_setup = await container.auth_service.needs_setup()
    settings = getattr(container, "app_settings", None)
    debug_ui_enabled = bool(getattr(settings, "debug_ui_enabled", False))
    cloud_mode = is_cloud_mode(container)
    return AuthConfigResponse(
        auth_enabled=_is_auth_enabled(container),
        needs_setup=False if cloud_mode else needs_setup,
        mode="cloud" if cloud_mode else "self_host",
        debug_ui_enabled=debug_ui_enabled,
        build_info=build_info_response(get_build_info()),
    )


@router.get("/demo/oauth/config", response_model=DemoOAuthConfigResponse)
async def get_demo_oauth_config(
    container: ServiceContainer = Depends(get_container),
) -> DemoOAuthConfigResponse:
    """Runtime public config for the demo OAuth SPA flow (cloud mode only).

    The SPA fetches the client id here before constructing the PKCE authorize
    URL, instead of relying on a value baked into the Vite build."""
    if not is_cloud_mode(container):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="demo oauth config is only available in cloud mode",
        )
    settings = getattr(container, "app_settings", None)
    demo_oauth = getattr(settings, "demo_oauth", None)
    discord = getattr(demo_oauth, "discord_client_id", "") if demo_oauth else ""
    google = getattr(demo_oauth, "google_client_id", "") if demo_oauth else ""
    return DemoOAuthConfigResponse(
        providers={
            "discord": DemoOAuthProviderConfigResponse(client_id=discord),
            "google": DemoOAuthProviderConfigResponse(client_id=google),
        },
    )


@router.get("/setup-status", response_model=SetupStatusResponse)
async def get_setup_status(
    container: ServiceContainer = Depends(get_container),
) -> SetupStatusResponse:
    svc = _require_auth_service(container)
    return SetupStatusResponse(needs_setup=await svc.needs_setup())


# ----------------------------------------------------------------------
# Setup (one-shot, public)
# ----------------------------------------------------------------------


@router.post(
    "/setup",
    response_model=AuthTokenResponse,
    status_code=status.HTTP_201_CREATED,
)
async def setup_initial_admin(
    payload: SetupRequest,
    request: Request,
    container: ServiceContainer = Depends(get_container),
    _self_host: None = Depends(require_self_host_mode),
) -> AuthTokenResponse:
    """One-shot endpoint to attach email + password to the default
    user. Fails 409 if already done — front-end then routes to /login.
    """
    svc = _require_auth_service(container)
    country_code, latitude, longitude, location_label = (
        _explicit_location_fields(payload) or (None, None, None, None)
    )
    try:
        user, token = await svc.setup_initial_admin(
            email=payload.email,
            password=payload.password,
            primary_language=payload.primary_language,
            timezone_id=payload.timezone_id,
            country_code=country_code,
            latitude=latitude,
            longitude=longitude,
            location_label=location_label,
        )
    except AuthError as exc:
        raise _translate_auth_error(exc) from exc
    return AuthTokenResponse(user=UserResponse.from_domain(user), token=token)


# ----------------------------------------------------------------------
# Login (public)
# ----------------------------------------------------------------------


@router.post("/login", response_model=AuthTokenResponse)
async def login(
    payload: LoginRequest,
    request: Request,
    container: ServiceContainer = Depends(get_container),
) -> AuthTokenResponse:
    strategy = _require_auth_strategy(container)
    try:
        user, token = await strategy.login(
            email=payload.email, password=payload.password,
        )
        user = await _seed_missing_location_from_login(
            user=user, request=request, container=container,
        )
    except AuthError as exc:
        raise _translate_auth_error(exc) from exc
    return AuthTokenResponse(user=UserResponse.from_domain(user), token=token)


@router.post("/demo/session", response_model=AuthTokenResponse)
async def create_demo_session_login(
    payload: DemoSessionLoginRequest,
    request: Request,
    container: ServiceContainer = Depends(get_container),
) -> AuthTokenResponse:
    if not is_cloud_mode(container):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="demo session login is only available in cloud mode",
        )
    strategy = _require_auth_strategy(container)
    location = await _location_from_request(request=request, container=container)
    try:
        user, token = await strategy.login_with_demo_session(
            provider=payload.provider,
            authorization_code=payload.authorization_code,
            redirect_uri=payload.redirect_uri,
            code_verifier=payload.code_verifier,
            source_ip=_trusted_edge_client_ip(request),
            device_id=request.headers.get("x-yuralume-demo-device"),
            profile_seed=_profile_seed_from_location(location),
        )
        user = await _seed_missing_location_from_login(
            user=user, request=request, container=container,
        )
    except AuthError as exc:
        raise _translate_auth_error(exc) from exc
    return AuthTokenResponse(user=UserResponse.from_domain(user), token=token)


# ----------------------------------------------------------------------
# Current user (bearer)
# ----------------------------------------------------------------------


@router.post("/cloud/session", response_model=AuthTokenResponse)
async def create_cloud_session_login(
    payload: CloudPlaySessionRequest,
    request: Request,
    container: ServiceContainer = Depends(get_container),
) -> AuthTokenResponse:
    """Exchange a portal-issued one-time hosted-play code for a Core JWT
    (plan H0). Cloud mode only — mirrors ``POST /auth/demo/session``: a
    GeoIP seed pins the operator's immutable timezone/location the first
    time an account enters, and application auth errors are translated to
    HTTP the same way."""
    if not is_cloud_mode(container):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="cloud session login is only available in cloud mode",
        )
    strategy = _require_auth_strategy(container)
    location = await _location_from_request(request=request, container=container)
    try:
        user, token = await strategy.login_with_cloud_play_code(
            code=payload.code,
            profile_seed=_profile_seed_from_location(location),
        )
        user = await _seed_missing_location_from_login(
            user=user, request=request, container=container,
        )
    except AuthError as exc:
        raise _translate_auth_error(exc) from exc
    return AuthTokenResponse(user=UserResponse.from_domain(user), token=token)


@router.get("/me", response_model=UserResponse)
async def get_me(
    user: OperatorProfile = Depends(get_current_user),
) -> UserResponse:
    """Return the bearer-token holder's profile. In disabled-auth
    mode, returns the default operator."""
    return UserResponse.from_domain(user)


@router.post("/me/password", response_model=UserResponse)
async def change_own_password(
    payload: ChangeOwnPasswordRequest,
    user: OperatorProfile = Depends(get_current_user),
    container: ServiceContainer = Depends(get_container),
    _self_host: None = Depends(require_self_host_mode),
) -> UserResponse:
    """Change the current user's password after verifying the current
    password. This is the player-facing self-service path."""
    svc = _require_auth_service(container)
    try:
        updated = await svc.change_own_password(
            actor=user,
            current_password=payload.current_password,
            new_password=payload.new_password,
        )
    except InvalidCredentials as exc:
        # The bearer token is valid; only the re-entered current
        # password failed. Return 400 so the frontend form can show an
        # inline error without the global 401 interceptor logging the
        # user out.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except AuthError as exc:
        raise _translate_auth_error(exc) from exc
    return UserResponse.from_domain(updated)


# ----------------------------------------------------------------------
# Admin: list / create / delete / change-password
# ----------------------------------------------------------------------


@router.get("/users", response_model=list[UserResponse])
async def list_users(
    admin: OperatorProfile = Depends(require_admin),
    container: ServiceContainer = Depends(get_container),
    _self_host: None = Depends(require_self_host_mode),
) -> list[UserResponse]:
    svc = _require_auth_service(container)
    users = await svc.list_users(actor=admin)
    return [UserResponse.from_domain(u) for u in users]


@router.post(
    "/users",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_user(
    payload: CreateUserRequest,
    request: Request,
    admin: OperatorProfile = Depends(require_admin),
    container: ServiceContainer = Depends(get_container),
    _self_host: None = Depends(require_self_host_mode),
) -> UserResponse:
    svc = _require_auth_service(container)
    country_code, latitude, longitude, location_label = (
        _explicit_location_fields(payload) or (None, None, None, None)
    )
    try:
        user = await svc.create_user(
            actor=admin,
            email=payload.email,
            password=payload.password,
            display_name=payload.display_name,
            is_admin=payload.is_admin,
            primary_language=payload.primary_language,
            timezone_id=payload.timezone_id,
            country_code=country_code,
            latitude=latitude,
            longitude=longitude,
            location_label=location_label,
        )
    except AuthError as exc:
        raise _translate_auth_error(exc) from exc
    return UserResponse.from_domain(user)


@router.delete(
    "/users/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_user(
    user_id: str,
    admin: OperatorProfile = Depends(require_admin),
    container: ServiceContainer = Depends(get_container),
    _self_host: None = Depends(require_self_host_mode),
) -> None:
    svc = _require_auth_service(container)
    try:
        await svc.delete_user(actor=admin, user_id=user_id)
    except AuthError as exc:
        raise _translate_auth_error(exc) from exc


class SetUserAdminRequest(BaseModel):
    is_admin: bool


@router.patch(
    "/users/{user_id}/admin",
    response_model=UserResponse,
)
async def set_user_admin(
    user_id: str,
    payload: SetUserAdminRequest,
    admin: OperatorProfile = Depends(require_admin),
    container: ServiceContainer = Depends(get_container),
    _self_host: None = Depends(require_self_host_mode),
) -> UserResponse:
    """Promote / demote an existing user's admin flag.

    Recovery path for a second account created without ticking Grant admin —
    admin surfaces (provider keys / BYOK, models, site settings) gate on
    ``is_admin`` alone, so a missed checkbox otherwise means recreating the
    user."""
    svc = _require_auth_service(container)
    try:
        updated = await svc.set_user_admin(
            actor=admin, user_id=user_id, is_admin=payload.is_admin,
        )
    except AuthError as exc:
        raise _translate_auth_error(exc) from exc
    return UserResponse.from_domain(updated)


@router.post(
    "/users/{user_id}/password",
    response_model=UserResponse,
)
async def change_password(
    user_id: str,
    payload: ChangePasswordRequest,
    admin: OperatorProfile = Depends(require_admin),
    container: ServiceContainer = Depends(get_container),
    _self_host: None = Depends(require_self_host_mode),
) -> UserResponse:
    """Admin reset for a user's password."""
    svc = _require_auth_service(container)
    try:
        updated = await svc.change_password(
            actor=admin, user_id=user_id, new_password=payload.new_password,
        )
    except AuthError as exc:
        raise _translate_auth_error(exc) from exc
    return UserResponse.from_domain(updated)
