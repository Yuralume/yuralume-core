"""StorySceneQuotaGuard — SC3-B per-tier daily ceiling on 起幕 openings.

Unit-only: the guard is deliberately independent of ``StorySceneService``
(see the module docstring), so these tests exercise it against stub
collaborators rather than the real session repository / character
repository. :mod:`test_story_scene_session_repository` covers
``count_opened_since`` itself against both real backends.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.application.services.story_scene_quota import (
    STORY_SCENE_DAILY_LIMIT_WINDOW,
    StorySceneDailyLimitReached,
    StorySceneQuotaGuard,
    StorySceneQuotaUnavailable,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.value_objects.account_runtime_profile import (
    DEFAULT_ACCOUNT_RUNTIME_PROFILE,
    AccountRuntimeProfile,
)
from kokoro_link.domain.value_objects.character_state import CharacterState

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
OPERATOR = "operator-1"


def _character(char_id: str = "char-1", *, user_id: str = OPERATOR) -> Character:
    state = CharacterState(
        emotion="calm", affection=50, fatigue=20, trust=50, energy=60,
    )
    return Character(
        id=char_id, name="Yui", summary="", personality=[], interests=[],
        speaking_style="", boundaries=[], state=state, user_id=user_id,
    )


class _StubProfiles:
    def __init__(self, profile: AccountRuntimeProfile) -> None:
        self._profile = profile
        self.calls: list[str] = []

    async def resolve_for_operator(self, operator_id: str) -> AccountRuntimeProfile:
        self.calls.append(operator_id)
        return self._profile


class _StubCharacters:
    def __init__(
        self, characters: list[Character] | None = None, *, fails: bool = False,
    ) -> None:
        self._characters = characters or []
        self._fails = fails
        self.calls: list[str] = []

    async def list_for_user(self, user_id: str) -> list[Character]:
        self.calls.append(user_id)
        if self._fails:
            raise RuntimeError("roster unreadable")
        return self._characters


class _StubSessions:
    def __init__(self, count: int = 0, *, fails: bool = False) -> None:
        self._count = count
        self._fails = fails
        self.calls: list[tuple[tuple[str, ...], datetime]] = []

    async def count_opened_since(
        self, character_ids, *, since: datetime,
    ) -> int:
        self.calls.append((tuple(sorted(character_ids)), since))
        if self._fails:
            raise RuntimeError("count unreadable")
        return self._count


def _profile(limit: int | None) -> AccountRuntimeProfile:
    return AccountRuntimeProfile(name="plus", story_scene_daily_limit=limit)


def _guard(
    *, profile: AccountRuntimeProfile, count: int = 0,
    session_fails: bool = False,
    characters: list[Character] | None = None,
    characters_fail: bool = False,
) -> tuple[StorySceneQuotaGuard, _StubSessions, _StubCharacters, _StubProfiles]:
    sessions = _StubSessions(count, fails=session_fails)
    chars = _StubCharacters(characters, fails=characters_fail)
    profiles = _StubProfiles(profile)
    guard = StorySceneQuotaGuard(sessions=sessions, characters=chars, profiles=profiles)
    return guard, sessions, chars, profiles


async def test_none_limit_allows_without_touching_sessions_or_characters() -> None:
    """The self-host / not-opted-in case (SC3-B): a ``None`` limit must be a
    zero-cost no-op, never reaching either dependency."""
    guard, sessions, chars, _ = _guard(profile=_profile(None))

    await guard.check(_character(), now=NOW)

    assert sessions.calls == []
    assert chars.calls == []


async def test_default_profile_has_no_limit() -> None:
    assert DEFAULT_ACCOUNT_RUNTIME_PROFILE.story_scene_daily_limit is None


async def test_under_limit_allows() -> None:
    guard, sessions, _, _ = _guard(profile=_profile(3), count=2)

    await guard.check(_character(), now=NOW)  # does not raise

    assert sessions.calls[0][1] == NOW - STORY_SCENE_DAILY_LIMIT_WINDOW


async def test_at_limit_raises_daily_limit_reached() -> None:
    guard, _, _, _ = _guard(profile=_profile(3), count=3)

    with pytest.raises(StorySceneDailyLimitReached) as caught:
        await guard.check(_character(), now=NOW)

    assert caught.value.limit == 3
    assert caught.value.used == 3
    assert caught.value.code == "story_scene_daily_limit_reached"


async def test_over_limit_also_raises() -> None:
    guard, _, _, _ = _guard(profile=_profile(3), count=5)

    with pytest.raises(StorySceneDailyLimitReached):
        await guard.check(_character(), now=NOW)


async def test_window_boundary_is_exactly_24h_before_now() -> None:
    guard, sessions, _, _ = _guard(profile=_profile(1), count=0)

    await guard.check(_character(), now=NOW)

    _, since = sessions.calls[0]
    assert since == NOW - timedelta(hours=24)


async def test_sums_across_a_players_characters() -> None:
    """The ceiling is per-player, not per-character (§4.1 SC3-B): the guard
    must ask the character repository for the operator's whole roster and
    pass every id to the session count."""
    other = _character("char-other", user_id=OPERATOR)
    guard, sessions, chars, _ = _guard(
        profile=_profile(5), count=4, characters=[_character("char-1"), other],
    )

    await guard.check(_character("char-1"), now=NOW)

    assert chars.calls == [OPERATOR]
    ids_used, _ = sessions.calls[0]
    assert set(ids_used) == {"char-1", "char-other"}


async def test_session_count_failure_denies_fail_closed() -> None:
    guard, _, _, _ = _guard(profile=_profile(5), session_fails=True)

    with pytest.raises(StorySceneQuotaUnavailable) as caught:
        await guard.check(_character(), now=NOW)

    assert caught.value.code == "story_scene_quota_unavailable"


async def test_character_roster_failure_denies_fail_closed() -> None:
    """A broken roster read must not silently narrow the count to just the
    one character opening the scene — that would undercount an operator
    with several and let them slip past an already-reached ceiling."""
    guard, _, _, _ = _guard(profile=_profile(5), characters_fail=True)

    with pytest.raises(StorySceneQuotaUnavailable):
        await guard.check(_character(), now=NOW)


async def test_no_now_argument_uses_the_real_clock() -> None:
    guard, sessions, _, _ = _guard(profile=_profile(1), count=0)

    before = datetime.now(timezone.utc)
    await guard.check(_character())
    after = datetime.now(timezone.utc)

    _, since = sessions.calls[0]
    assert before - STORY_SCENE_DAILY_LIMIT_WINDOW <= since <= after - STORY_SCENE_DAILY_LIMIT_WINDOW
