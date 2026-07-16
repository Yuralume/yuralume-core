"""Concurrent setup must produce exactly one winner (P1-4).

Two parallel ``setup_initial_admin`` callers should resolve to one
:class:`OperatorProfile` with the winner's password hash and one
:class:`SetupAlreadyComplete` raise. The race is guarded at the
repository via ``set_default_password_if_unset``.
"""

from __future__ import annotations

import asyncio

import pytest

from kokoro_link.application.services.auth_service import (
    AuthService,
    SetupAlreadyComplete,
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


@pytest.mark.asyncio
async def test_concurrent_setup_only_one_wins() -> None:
    repo = InMemoryOperatorProfileRepository()
    await repo.save(
        OperatorProfile(id=DEFAULT_OPERATOR_ID, display_name="default"),
    )
    service = AuthService(
        repository=repo,
        jwt_service=JWTService(
            secret="setup-race-test-secret-at-least-32-bytes",
        ),
        hasher=FakePasswordHasher(),
    )

    async def attempt(email: str, password: str):
        try:
            user, _ = await service.setup_initial_admin(
                email=email, password=password,
            )
            return ("ok", user.email)
        except SetupAlreadyComplete:
            return ("conflict", None)

    results = await asyncio.gather(
        attempt("alice@example.com", "alice-pw-12345"),
        attempt("bob@example.com", "bob-pw-12345"),
    )

    statuses = [r[0] for r in results]
    assert statuses.count("ok") == 1
    assert statuses.count("conflict") == 1

    winner_email = next(r[1] for r in results if r[0] == "ok")
    persisted = await repo.get(DEFAULT_OPERATOR_ID)
    assert persisted is not None
    assert persisted.email == winner_email


@pytest.mark.asyncio
async def test_setup_after_completed_setup_raises_already_complete() -> None:
    """Sanity check: the post-setup retry still raises, just from the
    pre-check path (since the row has a password now)."""
    repo = InMemoryOperatorProfileRepository()
    await repo.save(
        OperatorProfile(id=DEFAULT_OPERATOR_ID, display_name="default"),
    )
    service = AuthService(
        repository=repo,
        jwt_service=JWTService(
            secret="setup-race-test-secret-at-least-32-bytes",
        ),
        hasher=FakePasswordHasher(),
    )
    await service.setup_initial_admin(
        email="alice@example.com", password="alice-pw-12345",
    )
    with pytest.raises(SetupAlreadyComplete):
        await service.setup_initial_admin(
            email="bob@example.com", password="bob-pw-12345",
        )
