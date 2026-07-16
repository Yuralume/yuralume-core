"""Unit tests for ``QuietHoursService`` after the multi-user phase 2
move to ``PreferencesRepositoryPort`` + per-user override semantics
(HUMANIZATION_ROADMAP §4.5).

Coverage:

- env defaults when nothing is saved
- global preference overrides env
- user override overrides global, falls back to global / env
- malformed values fall back to env (legacy app_runtime_settings
  rows were strings, so the coercion path still tolerates strings)
- wrap-around windows (23–07) still work
- clear_user_window drops the override
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from kokoro_link.application.services.quiet_hours_service import (
    DEFAULT_QUIET_HOURS_END,
    DEFAULT_QUIET_HOURS_START,
    KEY_QUIET_HOURS_END,
    KEY_QUIET_HOURS_START,
    QuietHoursService,
    QuietHoursWindow,
)
from kokoro_link.application.services.scoped_preferences import (
    user_preference_key,
)
from kokoro_link.infrastructure.repositories.in_memory_preferences import (
    InMemoryPreferencesRepository,
)


def _service(
    *, prefs: InMemoryPreferencesRepository | None = None,
    env_start: int = DEFAULT_QUIET_HOURS_START,
    env_end: int = DEFAULT_QUIET_HOURS_END,
) -> QuietHoursService:
    return QuietHoursService(
        preferences=prefs if prefs is not None else InMemoryPreferencesRepository(),
        env_start=env_start,
        env_end=env_end,
    )


@pytest.mark.asyncio
async def test_falls_back_to_env_defaults_when_prefs_empty() -> None:
    svc = _service()
    window = await svc.window()
    assert window == QuietHoursWindow(
        start=DEFAULT_QUIET_HOURS_START, end=DEFAULT_QUIET_HOURS_END,
    )


@pytest.mark.asyncio
async def test_global_preference_overrides_env_default() -> None:
    prefs = InMemoryPreferencesRepository()
    await prefs.set(KEY_QUIET_HOURS_START, 1)
    await prefs.set(KEY_QUIET_HOURS_END, 9)
    svc = _service(prefs=prefs, env_start=2, env_end=6)
    window = await svc.window()
    assert window == QuietHoursWindow(start=1, end=9)


@pytest.mark.asyncio
async def test_user_override_beats_global() -> None:
    prefs = InMemoryPreferencesRepository()
    await prefs.set(KEY_QUIET_HOURS_START, 1)
    await prefs.set(KEY_QUIET_HOURS_END, 9)
    svc = _service(prefs=prefs, env_start=2, env_end=6)

    # Alice has her own window — should beat the global one above.
    await svc.set_window(start=22, end=5, user_id="alice")

    alice_window = await svc.window(user_id="alice")
    assert alice_window == QuietHoursWindow(start=22, end=5)
    # Bob has no override, so he sees the global default.
    bob_window = await svc.window(user_id="bob")
    assert bob_window == QuietHoursWindow(start=1, end=9)


@pytest.mark.asyncio
async def test_user_override_falls_through_to_env_when_no_global() -> None:
    svc = _service(env_start=2, env_end=6)
    # No global, no user override → env default.
    assert await svc.window(user_id="alice") == QuietHoursWindow(start=2, end=6)


@pytest.mark.asyncio
async def test_string_legacy_value_still_parses() -> None:
    """Legacy ``app_runtime_settings`` rows persisted hours as strings.

    A DB restore that copied those into ``app_preferences`` should
    still produce a usable window — the coercion path tolerates strings
    in the same shape ``_parse_hour`` always handled."""
    prefs = InMemoryPreferencesRepository()
    await prefs.set(KEY_QUIET_HOURS_START, "1")
    await prefs.set(KEY_QUIET_HOURS_END, "9")
    svc = _service(prefs=prefs, env_start=2, env_end=6)
    window = await svc.window()
    assert window == QuietHoursWindow(start=1, end=9)


@pytest.mark.asyncio
async def test_malformed_value_falls_back_to_env() -> None:
    prefs = InMemoryPreferencesRepository()
    await prefs.set(KEY_QUIET_HOURS_START, "not-a-number")
    svc = _service(prefs=prefs, env_start=2, env_end=6)
    window = await svc.window()
    assert window == QuietHoursWindow(start=2, end=6)


@pytest.mark.asyncio
async def test_in_quiet_hours_uses_user_window() -> None:
    prefs = InMemoryPreferencesRepository()
    svc = _service(prefs=prefs, env_start=2, env_end=6)
    await svc.set_window(start=14, end=17, user_id="alice")
    inside = datetime(2026, 1, 1, 15, 0, tzinfo=timezone.utc)
    outside = datetime(2026, 1, 1, 3, 0, tzinfo=timezone.utc)
    assert await svc.in_quiet_hours(user_id="alice", now=inside)
    assert not await svc.in_quiet_hours(user_id="alice", now=outside)


@pytest.mark.asyncio
async def test_in_quiet_hours_converts_now_to_user_timezone() -> None:
    prefs = InMemoryPreferencesRepository()
    svc = _service(prefs=prefs, env_start=2, env_end=6)
    await svc.set_window(start=2, end=6, user_id="alice")

    assert await svc.in_quiet_hours(
        user_id="alice",
        now=datetime(2026, 6, 14, 19, 0, tzinfo=timezone.utc),
        timezone_id="Asia/Taipei",
    )
    assert not await svc.in_quiet_hours(
        user_id="alice",
        now=datetime(2026, 6, 14, 19, 0, tzinfo=timezone.utc),
        timezone_id="UTC",
    )


@pytest.mark.asyncio
async def test_set_window_global_persists_unscoped_key() -> None:
    prefs = InMemoryPreferencesRepository()
    svc = _service(prefs=prefs)
    await svc.set_window(start=23, end=7)
    assert await prefs.get(KEY_QUIET_HOURS_START) == 23
    assert await prefs.get(KEY_QUIET_HOURS_END) == 7


@pytest.mark.asyncio
async def test_set_window_user_persists_scoped_key() -> None:
    prefs = InMemoryPreferencesRepository()
    svc = _service(prefs=prefs)
    await svc.set_window(start=22, end=5, user_id="alice")
    # Global key untouched — only the scoped key got a write.
    assert await prefs.get(KEY_QUIET_HOURS_START) is None
    scoped_start = user_preference_key("alice", KEY_QUIET_HOURS_START)
    scoped_end = user_preference_key("alice", KEY_QUIET_HOURS_END)
    assert await prefs.get(scoped_start) == 22
    assert await prefs.get(scoped_end) == 5


@pytest.mark.asyncio
async def test_window_wraps_midnight() -> None:
    svc = _service(env_start=23, env_end=6)
    assert await svc.in_quiet_hours(
        now=datetime(2026, 1, 1, 23, 30, tzinfo=timezone.utc),
    )
    assert await svc.in_quiet_hours(
        now=datetime(2026, 1, 2, 3, 0, tzinfo=timezone.utc),
    )
    assert not await svc.in_quiet_hours(
        now=datetime(2026, 1, 2, 12, 0, tzinfo=timezone.utc),
    )


@pytest.mark.asyncio
async def test_clear_user_window_restores_global_fallback() -> None:
    prefs = InMemoryPreferencesRepository()
    await prefs.set(KEY_QUIET_HOURS_START, 1)
    await prefs.set(KEY_QUIET_HOURS_END, 9)
    svc = _service(prefs=prefs)
    await svc.set_window(start=22, end=5, user_id="alice")
    assert await svc.window(user_id="alice") == QuietHoursWindow(start=22, end=5)

    removed = await svc.clear_user_window(user_id="alice")
    assert removed is True
    # Alice now sees the global default again.
    assert await svc.window(user_id="alice") == QuietHoursWindow(start=1, end=9)
    # Clearing twice is a no-op (returns False because both keys missing).
    assert (await svc.clear_user_window(user_id="alice")) is False


@pytest.mark.asyncio
async def test_hour_clamps_out_of_range() -> None:
    prefs = InMemoryPreferencesRepository()
    await prefs.set(KEY_QUIET_HOURS_START, -3)
    await prefs.set(KEY_QUIET_HOURS_END, 99)
    svc = _service(prefs=prefs)
    window = await svc.window()
    assert 0 <= window.start <= 23
    assert 0 <= window.end <= 23
