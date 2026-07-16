"""User-scoped preferences with installation-wide fallback.

``app_preferences`` is a small key/value table whose primary key is
limited to 64 chars, so user-scoped keys use a stable hash of the user
id instead of embedding arbitrary ids directly. The original unscoped
key remains the installation-wide default.
"""

from __future__ import annotations

from hashlib import sha256
from typing import Any

from kokoro_link.contracts.repositories import PreferencesRepositoryPort


def user_preference_key(user_id: str, key: str) -> str:
    """Return the storage key for a user's override of ``key``."""
    digest = sha256(user_id.encode("utf-8")).hexdigest()[:24]
    return f"u:{digest}:{key}"


async def get_preference_with_user_fallback(
    preferences: PreferencesRepositoryPort,
    key: str,
    *,
    user_id: str | None,
) -> Any:
    """Read a user's override; fall back to the global key when absent."""
    if user_id:
        scoped = await preferences.get(user_preference_key(user_id, key))
        if scoped is not None:
            return scoped
    return await preferences.get(key)


async def set_user_preference(
    preferences: PreferencesRepositoryPort,
    key: str,
    value: object,
    *,
    user_id: str,
) -> None:
    await preferences.set(user_preference_key(user_id, key), value)


async def delete_user_preference(
    preferences: PreferencesRepositoryPort,
    key: str,
    *,
    user_id: str,
) -> bool:
    return await preferences.delete(user_preference_key(user_id, key))
