"""Helpers for resolving the current operator's player-visible language."""

from __future__ import annotations

from kokoro_link.bootstrap.container import ServiceContainer


async def resolve_operator_primary_language(
    container: ServiceContainer,
    user_id: str,
) -> str:
    service = getattr(container, "operator_profile_service", None)
    if service is None:
        return "zh-TW"
    profile = await service.get_for_user(user_id)
    return getattr(profile, "primary_language", None) or "zh-TW"


async def resolve_stored_operator_primary_language(
    container: ServiceContainer,
    user_id: str,
) -> str:
    repository = getattr(container, "operator_profile_repository", None)
    if repository is None:
        return ""
    profile = await repository.get(user_id)
    return getattr(profile, "primary_language", None) or ""
