"""Authentication strategy port and self-host implementation."""

from __future__ import annotations

from typing import Protocol

from kokoro_link.application.services.auth_service import AuthService
from kokoro_link.domain.entities.operator_profile import OperatorProfile


class AuthStrategy(Protocol):
    async def login(
        self, *, email: str, password: str,
    ) -> tuple[OperatorProfile, str]:
        """Authenticate and return the operator plus core session token."""

    async def verify_token(self, token: str) -> OperatorProfile | None:
        """Resolve a core session token to an operator profile."""

    def allows_local_setup(self) -> bool:
        """Whether local first-run setup endpoints may mutate users."""

    def allows_user_crud(self) -> bool:
        """Whether local admin user-management endpoints may mutate users."""


class LocalAuthStrategy:
    """Thin adapter over the existing self-host AuthService."""

    def __init__(self, auth_service: AuthService) -> None:
        self._auth_service = auth_service

    async def login(
        self, *, email: str, password: str,
    ) -> tuple[OperatorProfile, str]:
        return await self._auth_service.login(email=email, password=password)

    async def verify_token(self, token: str) -> OperatorProfile | None:
        return await self._auth_service.verify_token(token)

    def allows_local_setup(self) -> bool:
        return True

    def allows_user_crud(self) -> bool:
        return True
