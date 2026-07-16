"""Bootstrap-admin first-run seed (BOOTSTRAP_ADMIN_* env)."""

from __future__ import annotations

import pytest

from kokoro_link.application.services.auth_service import AuthService
from kokoro_link.application.services.bootstrap_admin_seed import (
    seed_bootstrap_admin,
)
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
    return (
        AuthService(repository=repo, hasher=hasher, jwt_service=jwt),
        repo,
        hasher,
    )


async def _seed_default(repo: InMemoryOperatorProfileRepository) -> None:
    await repo.save(OperatorProfile(
        id=DEFAULT_OPERATOR_ID,
        display_name="操作者",
        email=None,
        password_hash=None,
        is_admin=True,
    ))


@pytest.mark.asyncio
async def test_seed_no_op_when_env_unset() -> None:
    svc, repo, _ = _build_service()
    await _seed_default(repo)
    seeded = await seed_bootstrap_admin(svc, email="", password="")
    assert seeded is False
    # Default user untouched.
    user = await repo.get(DEFAULT_OPERATOR_ID)
    assert user is not None and user.has_password() is False


@pytest.mark.asyncio
async def test_seed_no_op_when_password_blank() -> None:
    svc, repo, _ = _build_service()
    await _seed_default(repo)
    seeded = await seed_bootstrap_admin(
        svc, email="me@example.com", password="   ",
    )
    assert seeded is False
    user = await repo.get(DEFAULT_OPERATOR_ID)
    assert user is not None and user.has_password() is False


@pytest.mark.asyncio
async def test_seed_writes_when_default_unset() -> None:
    svc, repo, hasher = _build_service()
    await _seed_default(repo)
    seeded = await seed_bootstrap_admin(
        svc, email="admin@example.com", password="hunter2",
    )
    assert seeded is True
    user = await repo.get(DEFAULT_OPERATOR_ID)
    assert user is not None
    assert user.email == "admin@example.com"
    assert user.has_password()
    assert hasher.verify("hunter2", user.password_hash or "") is True
    assert user.is_admin is True


@pytest.mark.asyncio
async def test_seed_idempotent_after_manual_setup() -> None:
    """A second call (e.g. container restart) is a no-op when setup
    is already done — must not raise, must not overwrite."""
    svc, repo, _ = _build_service()
    await _seed_default(repo)
    # Simulate the operator already running /auth/setup by hand.
    await svc.setup_initial_admin(email="manual@example.com", password="orig")

    seeded = await seed_bootstrap_admin(
        svc, email="env@example.com", password="hunter2",
    )
    assert seeded is False
    user = await repo.get(DEFAULT_OPERATOR_ID)
    assert user is not None
    assert user.email == "manual@example.com"  # not overwritten


@pytest.mark.asyncio
async def test_seed_swallows_invalid_email() -> None:
    """Malformed email surfaces as InvalidCredentials inside the
    service; the seed must fail-soft so startup doesn't crash."""
    svc, repo, _ = _build_service()
    await _seed_default(repo)
    seeded = await seed_bootstrap_admin(
        svc, email="   ", password="hunter2",
    )
    assert seeded is False
    user = await repo.get(DEFAULT_OPERATOR_ID)
    assert user is not None and user.has_password() is False


@pytest.mark.asyncio
async def test_seed_swallows_missing_default_row() -> None:
    """Default user row missing (migration didn't run) — fail-soft."""
    svc, _repo, _ = _build_service()
    # No _seed_default() — repo is empty.
    seeded = await seed_bootstrap_admin(
        svc, email="admin@example.com", password="hunter2",
    )
    assert seeded is False
