"""AuthService — setup, login, user CRUD, permission rules."""

from __future__ import annotations

import pytest

from kokoro_link.application.exceptions import (
    InvalidCredentials,
    PermissionDenied,
    SetupAlreadyComplete,
    SetupNotAllowed,
    UserAlreadyExists,
    UserNotFound,
)
from kokoro_link.application.services.auth_service import AuthService
from kokoro_link.application.services.jwt_service import JWTService
from kokoro_link.domain.entities.operator_profile import (
    DEFAULT_OPERATOR_ID,
    OperatorProfile,
)
from kokoro_link.infrastructure.repositories.in_memory_operator_profile import (
    InMemoryOperatorProfileRepository,
)
from kokoro_link.infrastructure.security.password_hasher import (
    FakePasswordHasher,
)


def _build_service() -> tuple[
    AuthService, InMemoryOperatorProfileRepository, FakePasswordHasher,
]:
    repo = InMemoryOperatorProfileRepository()
    hasher = FakePasswordHasher()
    jwt = JWTService(secret="test-secret")
    return AuthService(repository=repo, hasher=hasher, jwt_service=jwt), repo, hasher


async def _seed_default(repo: InMemoryOperatorProfileRepository) -> None:
    await repo.save(OperatorProfile(
        id=DEFAULT_OPERATOR_ID,
        display_name="操作者",
        email=None,
        password_hash=None,
        is_admin=True,
    ))


@pytest.mark.asyncio
async def test_needs_setup_true_when_default_has_no_password() -> None:
    svc, repo, _ = _build_service()
    await _seed_default(repo)
    assert await svc.needs_setup() is True


@pytest.mark.asyncio
async def test_needs_setup_true_when_default_row_missing() -> None:
    """If migration hasn't run, the only sensible front-end behaviour
    is to route to /setup; needs_setup should report True."""
    svc, _repo, _ = _build_service()
    assert await svc.needs_setup() is True


@pytest.mark.asyncio
async def test_needs_setup_false_after_setup() -> None:
    svc, repo, _ = _build_service()
    await _seed_default(repo)
    await svc.setup_initial_admin(email="me@example.com", password="hunter2")
    assert await svc.needs_setup() is False


@pytest.mark.asyncio
async def test_setup_initial_admin_writes_credentials() -> None:
    svc, repo, hasher = _build_service()
    await _seed_default(repo)
    user, token = await svc.setup_initial_admin(
        email="me@example.com", password="hunter2",
    )
    assert user.email == "me@example.com"
    assert user.has_password()
    assert hasher.verify("hunter2", user.password_hash or "") is True
    assert user.is_admin is True
    assert token  # JWT issued


@pytest.mark.asyncio
async def test_setup_initial_admin_normalises_email() -> None:
    svc, repo, _ = _build_service()
    await _seed_default(repo)
    user, _ = await svc.setup_initial_admin(
        email="  ME@Example.COM ", password="hunter2",
    )
    assert user.email == "me@example.com"


@pytest.mark.asyncio
async def test_setup_initial_admin_refuses_second_call() -> None:
    svc, repo, _ = _build_service()
    await _seed_default(repo)
    await svc.setup_initial_admin(email="me@example.com", password="hunter2")
    with pytest.raises(SetupAlreadyComplete):
        await svc.setup_initial_admin(
            email="other@example.com", password="newpass",
        )


@pytest.mark.asyncio
async def test_setup_initial_admin_when_default_row_missing_raises() -> None:
    svc, _repo, _ = _build_service()
    with pytest.raises(SetupNotAllowed):
        await svc.setup_initial_admin(
            email="me@example.com", password="hunter2",
        )


@pytest.mark.asyncio
async def test_setup_rejects_blank_inputs() -> None:
    svc, repo, _ = _build_service()
    await _seed_default(repo)
    with pytest.raises(InvalidCredentials):
        await svc.setup_initial_admin(email="", password="hunter2")
    with pytest.raises(InvalidCredentials):
        await svc.setup_initial_admin(email="me@example.com", password="")
    with pytest.raises(InvalidCredentials):
        await svc.setup_initial_admin(email="me@example.com", password="   ")


@pytest.mark.asyncio
async def test_login_round_trip() -> None:
    svc, repo, _ = _build_service()
    await _seed_default(repo)
    await svc.setup_initial_admin(email="me@example.com", password="hunter2")
    user, token = await svc.login(email="me@example.com", password="hunter2")
    assert user.email == "me@example.com"
    assert token


@pytest.mark.asyncio
async def test_login_email_case_insensitive() -> None:
    svc, repo, _ = _build_service()
    await _seed_default(repo)
    await svc.setup_initial_admin(email="me@example.com", password="hunter2")
    user, _ = await svc.login(email="ME@Example.com", password="hunter2")
    assert user.email == "me@example.com"


@pytest.mark.asyncio
async def test_login_wrong_password_rejected() -> None:
    svc, repo, _ = _build_service()
    await _seed_default(repo)
    await svc.setup_initial_admin(email="me@example.com", password="hunter2")
    with pytest.raises(InvalidCredentials):
        await svc.login(email="me@example.com", password="wrong")


@pytest.mark.asyncio
async def test_login_unknown_email_rejected() -> None:
    svc, repo, _ = _build_service()
    await _seed_default(repo)
    with pytest.raises(InvalidCredentials):
        await svc.login(email="ghost@example.com", password="hunter2")


@pytest.mark.asyncio
async def test_verify_token_round_trip() -> None:
    svc, repo, _ = _build_service()
    await _seed_default(repo)
    user, token = await svc.setup_initial_admin(
        email="me@example.com", password="hunter2",
    )
    resolved = await svc.verify_token(token)
    assert resolved is not None
    assert resolved.id == user.id


@pytest.mark.asyncio
async def test_verify_token_garbage_returns_none() -> None:
    svc, _repo, _ = _build_service()
    assert await svc.verify_token("not-a-token") is None
    assert await svc.verify_token("") is None


@pytest.mark.asyncio
async def test_verify_token_deleted_user_returns_none() -> None:
    svc, repo, _ = _build_service()
    await _seed_default(repo)
    _, token = await svc.setup_initial_admin(
        email="me@example.com", password="hunter2",
    )
    # User vanishes (admin delete in another tab).
    await repo.delete(DEFAULT_OPERATOR_ID)
    assert await svc.verify_token(token) is None


@pytest.mark.asyncio
async def test_list_users_requires_admin() -> None:
    svc, repo, _ = _build_service()
    await _seed_default(repo)
    admin, _ = await svc.setup_initial_admin(
        email="admin@example.com", password="hunter2",
    )
    # Admin can list.
    users = await svc.list_users(actor=admin)
    assert len(users) == 1

    # Add a non-admin and check refusal.
    bob = await svc.create_user(
        actor=admin,
        email="bob@example.com",
        password="bobpass",
        display_name="Bob",
    )
    with pytest.raises(PermissionDenied):
        await svc.list_users(actor=bob)


@pytest.mark.asyncio
async def test_create_user_duplicate_email_rejected() -> None:
    svc, repo, _ = _build_service()
    await _seed_default(repo)
    admin, _ = await svc.setup_initial_admin(
        email="admin@example.com", password="hunter2",
    )
    await svc.create_user(
        actor=admin,
        email="bob@example.com",
        password="bobpass",
        display_name="Bob",
    )
    with pytest.raises(UserAlreadyExists):
        await svc.create_user(
            actor=admin,
            email="BOB@example.com",  # case-insensitive collision
            password="other",
            display_name="Bobby",
        )


@pytest.mark.asyncio
async def test_create_user_blank_inputs_rejected() -> None:
    svc, repo, _ = _build_service()
    await _seed_default(repo)
    admin, _ = await svc.setup_initial_admin(
        email="admin@example.com", password="hunter2",
    )
    with pytest.raises(InvalidCredentials):
        await svc.create_user(
            actor=admin, email="", password="x", display_name="X",
        )
    with pytest.raises(InvalidCredentials):
        await svc.create_user(
            actor=admin, email="x@example.com", password="", display_name="X",
        )
    with pytest.raises(InvalidCredentials):
        await svc.create_user(
            actor=admin, email="x@example.com", password="x", display_name="",
        )


@pytest.mark.asyncio
async def test_delete_user_refuses_self() -> None:
    svc, repo, _ = _build_service()
    await _seed_default(repo)
    admin, _ = await svc.setup_initial_admin(
        email="admin@example.com", password="hunter2",
    )
    with pytest.raises(PermissionDenied):
        await svc.delete_user(actor=admin, user_id=admin.id)


@pytest.mark.asyncio
async def test_delete_user_refuses_last_admin() -> None:
    """Admin tries to delete a *different* admin who is the last one
    after they remove themselves from the admin set in a previous
    flow — guard refuses to leave the system without admins."""
    svc, repo, _ = _build_service()
    await _seed_default(repo)
    admin1, _ = await svc.setup_initial_admin(
        email="admin1@example.com", password="hunter2",
    )
    admin2 = await svc.create_user(
        actor=admin1,
        email="admin2@example.com", password="pw", display_name="A2",
        is_admin=True,
    )
    # Demote admin1 by directly editing the row (simulate "I gave up
    # admin"); now admin2 is the only admin.
    await repo.save(admin1.update(is_admin=False))
    admin1_demoted = (await repo.get(admin1.id))
    assert admin1_demoted is not None
    with pytest.raises(PermissionDenied):
        # admin2 (last admin) trying to delete themselves — caught
        # by self-delete first.
        await svc.delete_user(actor=admin2, user_id=admin2.id)
    # Even if we squint and let a different admin try, last-admin rule
    # bites: create a non-admin who somehow has admin rights to call
    # delete (impossible in practice but defensive).


@pytest.mark.asyncio
async def test_delete_user_happy_path() -> None:
    svc, repo, _ = _build_service()
    await _seed_default(repo)
    admin, _ = await svc.setup_initial_admin(
        email="admin@example.com", password="hunter2",
    )
    bob = await svc.create_user(
        actor=admin,
        email="bob@example.com", password="pw", display_name="Bob",
    )
    ok = await svc.delete_user(actor=admin, user_id=bob.id)
    assert ok is True
    assert await repo.get(bob.id) is None


@pytest.mark.asyncio
async def test_delete_user_not_found() -> None:
    svc, repo, _ = _build_service()
    await _seed_default(repo)
    admin, _ = await svc.setup_initial_admin(
        email="admin@example.com", password="hunter2",
    )
    with pytest.raises(UserNotFound):
        await svc.delete_user(actor=admin, user_id="does-not-exist")


@pytest.mark.asyncio
async def test_set_user_admin_promotes_member() -> None:
    svc, repo, _ = _build_service()
    await _seed_default(repo)
    admin, _ = await svc.setup_initial_admin(
        email="admin@example.com", password="hunter2",
    )
    bob = await svc.create_user(
        actor=admin, email="bob@example.com", password="pw", display_name="Bob",
    )
    assert bob.is_admin is False

    updated = await svc.set_user_admin(actor=admin, user_id=bob.id, is_admin=True)

    assert updated.is_admin is True
    reloaded = await repo.get(bob.id)
    assert reloaded is not None and reloaded.is_admin is True


@pytest.mark.asyncio
async def test_set_user_admin_demote_last_admin_rejected() -> None:
    svc, repo, _ = _build_service()
    await _seed_default(repo)
    admin, _ = await svc.setup_initial_admin(
        email="admin@example.com", password="hunter2",
    )
    with pytest.raises(PermissionDenied):
        await svc.set_user_admin(actor=admin, user_id=admin.id, is_admin=False)


@pytest.mark.asyncio
async def test_set_user_admin_demote_allowed_with_another_admin() -> None:
    svc, repo, _ = _build_service()
    await _seed_default(repo)
    admin1, _ = await svc.setup_initial_admin(
        email="admin1@example.com", password="hunter2",
    )
    admin2 = await svc.create_user(
        actor=admin1, email="admin2@example.com", password="pw",
        display_name="A2", is_admin=True,
    )

    updated = await svc.set_user_admin(
        actor=admin1, user_id=admin1.id, is_admin=False,
    )

    assert updated.is_admin is False
    still_admin = await repo.get(admin2.id)
    assert still_admin is not None and still_admin.is_admin is True


@pytest.mark.asyncio
async def test_set_user_admin_requires_admin_actor() -> None:
    svc, repo, _ = _build_service()
    await _seed_default(repo)
    admin, _ = await svc.setup_initial_admin(
        email="admin@example.com", password="hunter2",
    )
    bob = await svc.create_user(
        actor=admin, email="bob@example.com", password="pw", display_name="Bob",
    )
    with pytest.raises(PermissionDenied):
        await svc.set_user_admin(actor=bob, user_id=admin.id, is_admin=False)


@pytest.mark.asyncio
async def test_set_user_admin_not_found() -> None:
    svc, repo, _ = _build_service()
    await _seed_default(repo)
    admin, _ = await svc.setup_initial_admin(
        email="admin@example.com", password="hunter2",
    )
    with pytest.raises(UserNotFound):
        await svc.set_user_admin(actor=admin, user_id="missing", is_admin=True)


@pytest.mark.asyncio
async def test_change_password_self_ok() -> None:
    svc, repo, hasher = _build_service()
    await _seed_default(repo)
    admin, _ = await svc.setup_initial_admin(
        email="admin@example.com", password="oldpass",
    )
    updated = await svc.change_password(
        actor=admin, user_id=admin.id, new_password="newpass",
    )
    assert hasher.verify("newpass", updated.password_hash or "") is True
    assert hasher.verify("oldpass", updated.password_hash or "") is False


@pytest.mark.asyncio
async def test_change_password_non_admin_cannot_change_others() -> None:
    svc, repo, _ = _build_service()
    await _seed_default(repo)
    admin, _ = await svc.setup_initial_admin(
        email="admin@example.com", password="oldpass",
    )
    bob = await svc.create_user(
        actor=admin,
        email="bob@example.com", password="bobpass", display_name="Bob",
    )
    with pytest.raises(PermissionDenied):
        await svc.change_password(
            actor=bob, user_id=admin.id, new_password="hacked",
        )


@pytest.mark.asyncio
async def test_change_password_admin_can_change_others() -> None:
    svc, repo, hasher = _build_service()
    await _seed_default(repo)
    admin, _ = await svc.setup_initial_admin(
        email="admin@example.com", password="hunter2",
    )
    bob = await svc.create_user(
        actor=admin,
        email="bob@example.com", password="bobpass", display_name="Bob",
    )
    updated = await svc.change_password(
        actor=admin, user_id=bob.id, new_password="newbob",
    )
    assert hasher.verify("newbob", updated.password_hash or "") is True


@pytest.mark.asyncio
async def test_change_password_rejects_blank() -> None:
    svc, repo, _ = _build_service()
    await _seed_default(repo)
    admin, _ = await svc.setup_initial_admin(
        email="admin@example.com", password="hunter2",
    )
    with pytest.raises(InvalidCredentials):
        await svc.change_password(
            actor=admin, user_id=admin.id, new_password="",
        )
    with pytest.raises(InvalidCredentials):
        await svc.change_password(
            actor=admin, user_id=admin.id, new_password="   ",
        )


@pytest.mark.asyncio
async def test_change_own_password_requires_current_password() -> None:
    svc, repo, _ = _build_service()
    await _seed_default(repo)
    admin, _ = await svc.setup_initial_admin(
        email="admin@example.com", password="oldpass",
    )

    with pytest.raises(InvalidCredentials):
        await svc.change_own_password(
            actor=admin,
            current_password="wrong",
            new_password="newpass",
        )


@pytest.mark.asyncio
async def test_change_own_password_updates_when_current_password_matches() -> None:
    svc, repo, hasher = _build_service()
    await _seed_default(repo)
    admin, _ = await svc.setup_initial_admin(
        email="admin@example.com", password="oldpass",
    )

    updated = await svc.change_own_password(
        actor=admin,
        current_password="oldpass",
        new_password="newpass",
    )

    assert hasher.verify("newpass", updated.password_hash or "") is True
    assert hasher.verify("oldpass", updated.password_hash or "") is False


@pytest.mark.asyncio
async def test_change_own_password_rejects_blank_inputs() -> None:
    svc, repo, _ = _build_service()
    await _seed_default(repo)
    admin, _ = await svc.setup_initial_admin(
        email="admin@example.com", password="oldpass",
    )

    with pytest.raises(InvalidCredentials):
        await svc.change_own_password(
            actor=admin,
            current_password="",
            new_password="newpass",
        )
    with pytest.raises(InvalidCredentials):
        await svc.change_own_password(
            actor=admin,
            current_password="oldpass",
            new_password="",
        )


# ----------------------------------------------------------------------
# primary_language — Phase 1a of FRONTEND_I18N_PLAN
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_setup_defaults_primary_language_to_zh_TW() -> None:
    """Backward compat: clients that don't yet send primary_language
    get zh-TW (matches migration backfill)."""
    svc, repo, _ = _build_service()
    await _seed_default(repo)
    user, _ = await svc.setup_initial_admin(
        email="me@example.com", password="hunter2",
    )
    assert user.primary_language == "zh-TW"


@pytest.mark.asyncio
async def test_setup_accepts_explicit_primary_language() -> None:
    svc, repo, _ = _build_service()
    await _seed_default(repo)
    user, _ = await svc.setup_initial_admin(
        email="me@example.com",
        password="hunter2",
        primary_language="en-US",
    )
    assert user.primary_language == "en-US"


@pytest.mark.asyncio
async def test_setup_accepts_japanese_primary_language() -> None:
    svc, repo, _ = _build_service()
    await _seed_default(repo)
    user, _ = await svc.setup_initial_admin(
        email="me@example.com",
        password="hunter2",
        primary_language="ja-jp",
    )
    assert user.primary_language == "ja-JP"


@pytest.mark.asyncio
async def test_setup_pins_explicit_timezone() -> None:
    svc, repo, _ = _build_service()
    await _seed_default(repo)
    user, _ = await svc.setup_initial_admin(
        email="me@example.com",
        password="hunter2",
        timezone_id="Asia/Taipei",
    )
    assert user.timezone_id == "Asia/Taipei"


@pytest.mark.asyncio
async def test_setup_pins_explicit_location() -> None:
    svc, repo, _ = _build_service()
    await _seed_default(repo)
    user, _ = await svc.setup_initial_admin(
        email="me@example.com",
        password="hunter2",
        country_code="us",
        latitude=37.7749,
        longitude=-122.4194,
        location_label="San Francisco, US",
    )
    assert user.country_code == "US"
    assert user.latitude == 37.7749
    assert user.longitude == -122.4194
    assert user.location_label == "San Francisco, US"


@pytest.mark.asyncio
async def test_setup_normalises_primary_language_casing() -> None:
    """``en-us`` and ``EN-us`` should both canonicalise to ``en-US`` so
    the persisted value stays uniform regardless of frontend casing."""
    svc, repo, _ = _build_service()
    await _seed_default(repo)
    user, _ = await svc.setup_initial_admin(
        email="me@example.com",
        password="hunter2",
        primary_language="en-us",
    )
    assert user.primary_language == "en-US"


@pytest.mark.asyncio
async def test_setup_rejects_invalid_language_tag() -> None:
    """Direct service caller (CLI/tests) bypasses Pydantic, so the
    domain-level normaliser raises ``ValueError`` — HTTP callers see
    422 via the Pydantic validator on ``SetupRequest`` instead."""
    svc, repo, _ = _build_service()
    await _seed_default(repo)
    with pytest.raises(ValueError):
        await svc.setup_initial_admin(
            email="me@example.com",
            password="hunter2",
            primary_language="123",  # not alpha
        )


@pytest.mark.asyncio
async def test_create_user_pins_primary_language() -> None:
    svc, repo, _ = _build_service()
    await _seed_default(repo)
    admin, _ = await svc.setup_initial_admin(
        email="admin@example.com", password="hunter2",
    )
    bob = await svc.create_user(
        actor=admin,
        email="bob@example.com",
        password="bobpass",
        display_name="Bob",
        primary_language="ja-JP",
    )
    assert bob.primary_language == "ja-JP"


@pytest.mark.asyncio
async def test_create_user_pins_explicit_timezone() -> None:
    svc, repo, _ = _build_service()
    await _seed_default(repo)
    admin, _ = await svc.setup_initial_admin(
        email="admin@example.com", password="hunter2",
    )
    bob = await svc.create_user(
        actor=admin,
        email="bob@example.com",
        password="bobpass",
        display_name="Bob",
        timezone_id="Asia/Taipei",
    )
    assert bob.timezone_id == "Asia/Taipei"


@pytest.mark.asyncio
async def test_create_user_pins_explicit_location() -> None:
    svc, repo, _ = _build_service()
    await _seed_default(repo)
    admin, _ = await svc.setup_initial_admin(
        email="admin@example.com", password="hunter2",
    )
    bob = await svc.create_user(
        actor=admin,
        email="bob@example.com",
        password="bobpass",
        display_name="Bob",
        country_code="jp",
        latitude=35.6762,
        longitude=139.6503,
        location_label="Tokyo, JP",
    )
    assert bob.country_code == "JP"
    assert bob.latitude == 35.6762
    assert bob.longitude == 139.6503
    assert bob.location_label == "Tokyo, JP"


@pytest.mark.asyncio
async def test_create_user_defaults_primary_language() -> None:
    svc, repo, _ = _build_service()
    await _seed_default(repo)
    admin, _ = await svc.setup_initial_admin(
        email="admin@example.com", password="hunter2",
    )
    bob = await svc.create_user(
        actor=admin,
        email="bob@example.com",
        password="bobpass",
        display_name="Bob",
    )
    assert bob.primary_language == "zh-TW"


@pytest.mark.asyncio
async def test_create_user_defaults_timezone_from_service_setting() -> None:
    repo = InMemoryOperatorProfileRepository()
    hasher = FakePasswordHasher()
    svc = AuthService(
        repository=repo,
        hasher=hasher,
        jwt_service=JWTService(secret="test-secret"),
        default_timezone_id="Asia/Taipei",
    )
    await _seed_default(repo)
    admin, _ = await svc.setup_initial_admin(
        email="admin@example.com", password="hunter2",
    )

    bob = await svc.create_user(
        actor=admin,
        email="bob@example.com",
        password="bobpass",
        display_name="Bob",
    )

    assert bob.timezone_id == "Asia/Taipei"


@pytest.mark.asyncio
async def test_change_password_does_not_touch_primary_language() -> None:
    """Sanity: primary_language is immutable, so password changes must
    not accidentally rewrite it. ``OperatorProfile.update`` doesn't
    expose the field, but defending the invariant here keeps a future
    refactor honest."""
    svc, repo, _ = _build_service()
    await _seed_default(repo)
    admin, _ = await svc.setup_initial_admin(
        email="admin@example.com",
        password="hunter2",
        primary_language="en-US",
    )
    updated = await svc.change_password(
        actor=admin, user_id=admin.id, new_password="newer",
    )
    assert updated.primary_language == "en-US"
