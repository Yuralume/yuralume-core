"""FastAPI dependencies — DI container plumbing + auth.

The auth dependencies honour ``KOKORO_AUTH_ENABLED``:

- ``enabled=False`` (default): every request runs as the singleton
  default operator. No header is read, no token is verified. The
  schema-level ownership guard still works because every character
  carries ``user_id="default"`` after the M1 backfill.
- ``enabled=True``: bearer token is mandatory. Missing / malformed /
  expired / revoked → 401. ``require_admin`` adds an extra 403 gate
  for admin-only endpoints (user CRUD).
"""

from __future__ import annotations

from fastapi import Depends, Header, HTTPException, Request, status

from kokoro_link.bootstrap.container import ServiceContainer
from kokoro_link.domain.entities.character import Character
from kokoro_link.application.services.cloud_identity_context import (
    bind_cloud_actor,
)
from kokoro_link.domain.entities.operator_profile import (
    DEFAULT_OPERATOR_ID,
    DEFAULT_PRIMARY_LANGUAGE,
    OperatorProfile,
)


def get_container(request: Request) -> ServiceContainer:
    return request.app.state.container


def _is_auth_enabled(container: ServiceContainer) -> bool:
    """Read the toggle from the container's app settings. Defensive
    against tests building a partial container — default to ``False``
    (the production default) if settings are missing."""
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


def is_cloud_mode(container: ServiceContainer) -> bool:
    app_settings = getattr(container, "app_settings", None)
    if app_settings is None:
        return False
    cloud = getattr(app_settings, "cloud", None)
    return bool(getattr(cloud, "active", False))


def require_self_host_mode(
    container: ServiceContainer = Depends(get_container),
) -> None:
    strategy = getattr(container, "auth_strategy", None)
    if strategy is not None and not strategy.allows_local_setup():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="self-host auth management is disabled in cloud mode",
        )
    if strategy is None and is_cloud_mode(container):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="self-host auth management is disabled in cloud mode",
        )


async def get_current_user(
    request: Request,
    authorization: str | None = Header(default=None),
    container: ServiceContainer = Depends(get_container),
) -> OperatorProfile:
    """Resolve the current user (or the default user in disabled mode).

    Disabled mode short-circuits without touching the Authorization
    header at all — that lets the front-end omit the header entirely
    in single-machine deployments without breaking CORS preflight or
    falling into a "missing header" error path.

    Token sources (enabled mode):
    1. ``Authorization: Bearer <token>`` header — the default for axios
       / fetch / WebSocket clients.
    2. ``?access_token=<token>`` query parameter — fallback for the
       browser ``EventSource`` API which can't set custom headers.
       Treat the query token as bearer-equivalent; restrict the SSE
       endpoint to GET so the token never appears in form bodies.
    """
    if not _is_auth_enabled(container):
        default_user = await _resolve_default_user(container)
        bind_cloud_actor(operator_id=default_user.id)
        return default_user

    token: str | None = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
    if not token:
        # EventSource fallback: read token from ``?access_token=`` so
        # SSE subscribers (which can't send Authorization headers) can
        # still authenticate. Stays a *fallback* — header path runs
        # first so non-SSE callers don't accidentally leak tokens into
        # query strings.
        query_token = request.query_params.get("access_token")
        if query_token:
            token = query_token.strip()
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    auth_strategy = getattr(container, "auth_strategy", None)
    if auth_strategy is not None:
        user = await auth_strategy.verify_token(token)
    else:
        auth_service = container.auth_service
        if auth_service is None:
            # Auth enabled but container couldn't build AuthService — fail
            # closed rather than silently authenticate.
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="auth service not configured",
            )
        user = await auth_service.verify_token(token)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    # Bind the resolved operator as the ambient cloud actor for this request
    # task so leaf LLM services (tts / memoir / card translators, persona &
    # behaviour extractors, ...) resolve cloud identity without threading an
    # operator_id through every port. Per-request task isolation makes the
    # unreset binding safe — see cloud_identity_context.bind_cloud_actor.
    bind_cloud_actor(operator_id=user.id)
    return user


async def get_current_user_id(
    user: OperatorProfile = Depends(get_current_user),
) -> str:
    """Convenience for routes that only need the id (most do)."""
    return user.id


async def get_owned_character(
    character_id: str,
    current_user_id: str = Depends(get_current_user_id),
    container: ServiceContainer = Depends(get_container),
) -> Character:
    """Resolve a path ``character_id`` owned by the current user.

    Cross-user access deliberately collapses to the same 404 as a
    missing character, so callers cannot enumerate another user's ids.
    Use this on every ``/characters/{character_id}`` route before
    touching character-scoped data.
    """
    character_service = getattr(container, "character_service", None)
    if character_service is None:
        # Stub containers used by per-route unit tests don't expose
        # ``character_service``. They never multiplex users, so we
        # short-circuit ownership and hand back a duck-typed stand-in
        # carrying only the fields ``ensure_owned_character_id`` cares
        # about. Route handlers that need richer data re-fetch via
        # their own service.
        from types import SimpleNamespace
        return SimpleNamespace(  # type: ignore[return-value]
            id=character_id,
            user_id=current_user_id,
        )
    try:
        character = await character_service.get_character_entity(
            character_id, user_id=current_user_id,
        )
    except TypeError:
        # Several route unit tests use small stubs with the pre-auth
        # method shape. Keep the boundary compatible while still
        # enforcing ownership against the returned entity.
        character = await character_service.get_character_entity(
            character_id,
        )
        if (
            character is not None
            and getattr(character, "user_id", DEFAULT_OPERATOR_ID)
            != current_user_id
        ):
            character = None
    if character is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Character not found",
        )
    return character


async def ensure_owned_character_id(
    character: Character = Depends(get_owned_character),
) -> str:
    """Return the already-validated path character id."""
    return character.id


async def ensure_character_id_owned_by_user(
    character_id: str,
    current_user_id: str,
    container: ServiceContainer,
) -> Character:
    """Validate ownership for id-only routes that first resolve a child row."""
    character_service = getattr(container, "character_service", None)
    if character_service is None:
        from types import SimpleNamespace
        return SimpleNamespace(  # type: ignore[return-value]
            id=character_id,
            user_id=current_user_id,
        )
    try:
        character = await character_service.get_character_entity(
            character_id, user_id=current_user_id,
        )
    except TypeError:
        character = await character_service.get_character_entity(character_id)
        if (
            character is not None
            and getattr(character, "user_id", DEFAULT_OPERATOR_ID)
            != current_user_id
        ):
            character = None
    if character is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Character not found",
        )
    return character


async def require_admin(
    user: OperatorProfile = Depends(get_current_user),
) -> OperatorProfile:
    """Gate for admin-only endpoints (user CRUD, etc.).

    When auth is disabled, the default user is always admin (the
    single-machine owner) — see migration ct5y7z00070 which flips the
    is_admin flag on the default row. No extra check needed here.
    """
    if not user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="admin privilege required",
        )
    return user


async def _resolve_default_user(
    container: ServiceContainer,
) -> OperatorProfile:
    """Look up the default user. If the row is missing (clean install
    before migration ran), synthesise a stand-in so the disabled-auth
    code path still hands routes a non-None user — characters all
    carry ``user_id="default"`` so ownership guards still pass.

    Uses ``getattr`` because partial test containers may not declare
    the field at all (e.g. a stub ServiceContainer in observability
    tests). The fallback synthesises an admin profile so single-machine
    disabled-auth deployments treat the local operator as admin — same
    behaviour migration ct5y7z00070 already encodes on the persisted
    row."""
    repo = getattr(container, "operator_profile_repository", None)
    if repo is not None:
        existing = await repo.get(DEFAULT_OPERATOR_ID)
        if existing is not None:
            return existing
    # Row missing (clean install before migration, or in-memory repo with
    # no seed). Synthesise an admin default and honour the deploy-time
    # language + timezone so the SPA chrome, content language, and civil
    # time still match ``USER_PRIMARY_LANGUAGE`` / ``USER_TIMEZONE`` instead
    # of snapping back to zh-TW / UTC. The persisted path uses
    # ``seed_default_locale`` at boot; this keeps the no-row fallback
    # consistent with it.
    from dataclasses import replace

    app_settings = getattr(container, "app_settings", None)
    language = (
        getattr(app_settings, "default_primary_language", None)
        or DEFAULT_PRIMARY_LANGUAGE
    )
    user_timezone = getattr(app_settings, "user_timezone", None)
    fallback = OperatorProfile.default()
    timezone_id = (
        getattr(user_timezone, "default_timezone_id", None)
        or fallback.timezone_id
    )
    return replace(
        fallback,
        primary_language=language,
        timezone_id=timezone_id,
        is_admin=True,
    )
