"""PlayerRuntimeLimitsService — the display-only view of hosted ceilings.

The direction that matters here is the *opposite* of every guard these
numbers mirror: the guards fail **closed** (an unreadable ledger denies),
this service fails **soft** (an unreadable ledger reports ``used=None`` and
never raises). Enforcement already exists in the services; this surface only
exists so the player is told before they press the button, and a broken
counter must degrade to "we don't know" rather than taking the hint — or the
page — down.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.application.services.character_service import (
    CHARACTER_CREATE_LIMIT_WINDOW,
)
from kokoro_link.application.services.player_runtime_limits import (
    PlayerRuntimeLimitsService,
    QuotaUsage,
)
from kokoro_link.application.services.story_scene_quota import (
    STORY_SCENE_DAILY_LIMIT_WINDOW,
)
from kokoro_link.contracts.account_runtime_usage import (
    ACCOUNT_RUNTIME_EVENT_CHARACTER_CREATE,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.story_scene_session import (
    SCENE_LAYER_SIDE_STORY,
    StorySceneSession,
)
from kokoro_link.infrastructure.repositories.in_memory_account_runtime_usage import (
    InMemoryAccountRuntimeUsageRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_story_scene_sessions import (
    InMemoryStorySceneSessionRepository,
)
from kokoro_link.domain.value_objects.account_runtime_profile import (
    DEFAULT_ACCOUNT_RUNTIME_PROFILE,
    AccountRuntimeProfile,
)
from tests.unit._runtime_profiles import (
    RESTRICTIVE_ACCOUNT_RUNTIME_PROFILE,
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
        self._characters = characters if characters is not None else []
        self._fails = fails
        self.calls: list[str] = []

    async def list_for_user(self, user_id: str) -> list[Character]:
        self.calls.append(user_id)
        if self._fails:
            raise RuntimeError("roster unreadable")
        return self._characters


class _StubUsage:
    def __init__(self, count: int = 0, *, fails: bool = False) -> None:
        self._count = count
        self._fails = fails
        self.calls: list[dict[str, object]] = []

    async def count_events(
        self,
        *,
        operator_id: str,
        event_type: str,
        since: datetime,
        until: datetime | None = None,
        resource_id: str | None = None,
    ) -> int:
        self.calls.append({
            "operator_id": operator_id,
            "event_type": event_type,
            "since": since,
            "until": until,
        })
        if self._fails:
            raise RuntimeError("usage ledger unreadable")
        return self._count


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
            raise RuntimeError("scene count unreadable")
        return self._count


class _StubClock:
    def __init__(self, moment: datetime) -> None:
        self._moment = moment

    def now(self) -> datetime:
        return self._moment


def _service(
    profile: AccountRuntimeProfile,
    *,
    characters: _StubCharacters | None = None,
    usage: _StubUsage | None = None,
    sessions: _StubSessions | None = None,
    clock: _StubClock | None = None,
    omit_characters: bool = False,
    omit_usage: bool = False,
    omit_sessions: bool = False,
) -> tuple[
    PlayerRuntimeLimitsService,
    _StubCharacters | None,
    _StubUsage | None,
    _StubSessions | None,
]:
    chars = None if omit_characters else (characters or _StubCharacters())
    usage_repo = None if omit_usage else (usage or _StubUsage())
    session_repo = None if omit_sessions else (sessions or _StubSessions())
    service = PlayerRuntimeLimitsService(
        profiles=_StubProfiles(profile),
        characters=chars,
        usage_repository=usage_repo,
        story_scene_sessions=session_repo,
        clock=clock,
    )
    return service, chars, usage_repo, session_repo


def _limited() -> AccountRuntimeProfile:
    return AccountRuntimeProfile(
        name="plus",
        max_characters=3,
        daily_character_create_limit=1,
        story_scene_daily_limit=5,
        max_messages_per_session=80,
        album_generation_enabled=False,
        tts_enabled=False,
        video_generation_enabled=False,
    )


# --- unlimited (self-host / permissive tier) --------------------------------


async def test_permissive_profile_reports_every_quota_as_none() -> None:
    """``None`` limit is the only spelling of unlimited, and it must cost
    nothing: no roster read, no ledger read, no session count."""
    service, chars, usage, sessions = _service(DEFAULT_ACCOUNT_RUNTIME_PROFILE)

    snapshot = await service.snapshot_for_operator(OPERATOR, now=NOW)

    assert snapshot.character_slots is None
    assert snapshot.character_daily_creates is None
    assert snapshot.story_scenes_daily is None
    assert snapshot.session_message_limit is None
    assert snapshot.album_generation_enabled is True
    assert snapshot.tts_enabled is True
    assert snapshot.video_generation_enabled is True
    assert chars is not None and chars.calls == []
    assert usage is not None and usage.calls == []
    assert sessions is not None and sessions.calls == []


# --- limited tier: correct counts -------------------------------------------


async def test_limited_profile_counts_character_slots() -> None:
    service, chars, _, _ = _service(
        _limited(),
        characters=_StubCharacters([_character("char-1"), _character("char-2")]),
    )

    snapshot = await service.snapshot_for_operator(OPERATOR, now=NOW)

    assert snapshot.character_slots == QuotaUsage(limit=3, used=2)
    assert chars is not None and chars.calls[0] == OPERATOR


async def test_limited_profile_counts_daily_creates_over_a_rolling_24h() -> None:
    service, _, usage, _ = _service(_limited(), usage=_StubUsage(1))

    snapshot = await service.snapshot_for_operator(OPERATOR, now=NOW)

    assert snapshot.character_daily_creates == QuotaUsage(limit=1, used=1)
    assert usage is not None
    call = usage.calls[0]
    assert call["operator_id"] == OPERATOR
    assert call["event_type"] == ACCOUNT_RUNTIME_EVENT_CHARACTER_CREATE
    assert call["since"] == NOW - CHARACTER_CREATE_LIMIT_WINDOW
    assert call["since"] == NOW - timedelta(hours=24)
    assert call["until"] == NOW


async def test_limited_profile_counts_story_scenes_over_the_shared_window() -> None:
    """The window constant is imported from the guard, not re-typed here: the
    hint and the wall must move together or the hint becomes a lie."""
    service, _, _, sessions = _service(
        _limited(),
        characters=_StubCharacters([_character("char-1")]),
        sessions=_StubSessions(2),
    )

    snapshot = await service.snapshot_for_operator(OPERATOR, now=NOW)

    assert snapshot.story_scenes_daily == QuotaUsage(limit=5, used=2)
    assert sessions is not None
    _, since = sessions.calls[0]
    assert since == NOW - STORY_SCENE_DAILY_LIMIT_WINDOW


async def test_story_scene_count_spans_every_character_of_the_operator() -> None:
    """SC3-B's ceiling is per-player: the hint must count the same set the
    guard counts, or a two-character player is quoted the wrong number."""
    service, _, _, sessions = _service(
        _limited(),
        characters=_StubCharacters([
            _character("char-1"), _character("char-2"), _character("char-3"),
        ]),
        sessions=_StubSessions(4),
    )

    await service.snapshot_for_operator(OPERATOR, now=NOW)

    assert sessions is not None
    ids_used, _ = sessions.calls[0]
    assert set(ids_used) == {"char-1", "char-2", "char-3"}


async def test_empty_roster_reports_zero_scenes_without_querying() -> None:
    service, _, _, sessions = _service(
        _limited(), characters=_StubCharacters([]), sessions=_StubSessions(9),
    )

    snapshot = await service.snapshot_for_operator(OPERATOR, now=NOW)

    assert snapshot.story_scenes_daily == QuotaUsage(limit=5, used=0)
    assert sessions is not None and sessions.calls == []


async def test_flags_and_session_limit_come_straight_from_the_profile() -> None:
    service, _, _, _ = _service(RESTRICTIVE_ACCOUNT_RUNTIME_PROFILE)

    snapshot = await service.snapshot_for_operator(OPERATOR, now=NOW)

    assert snapshot.session_message_limit == 80
    assert snapshot.album_generation_enabled is False
    assert snapshot.tts_enabled is False
    assert snapshot.video_generation_enabled is False


# --- fail-soft: a broken counter degrades, it does not raise -----------------


async def test_roster_read_failure_reports_unknown_slot_usage() -> None:
    service, _, _, _ = _service(
        _limited(), characters=_StubCharacters(fails=True),
    )

    snapshot = await service.snapshot_for_operator(OPERATOR, now=NOW)

    assert snapshot.character_slots == QuotaUsage(limit=3, used=None)


async def test_roster_read_failure_also_leaves_scene_usage_unknown() -> None:
    """The scene count needs the roster too — a failed roster read must not
    silently narrow the count to nothing and quote ``0/5``."""
    service, _, _, sessions = _service(
        _limited(),
        characters=_StubCharacters(fails=True),
        sessions=_StubSessions(3),
    )

    snapshot = await service.snapshot_for_operator(OPERATOR, now=NOW)

    assert snapshot.story_scenes_daily == QuotaUsage(limit=5, used=None)
    assert sessions is not None and sessions.calls == []


async def test_usage_ledger_failure_reports_unknown_create_usage() -> None:
    service, _, _, _ = _service(_limited(), usage=_StubUsage(fails=True))

    snapshot = await service.snapshot_for_operator(OPERATOR, now=NOW)

    assert snapshot.character_daily_creates == QuotaUsage(limit=1, used=None)


async def test_scene_count_failure_reports_unknown_scene_usage() -> None:
    service, _, _, _ = _service(
        _limited(),
        characters=_StubCharacters([_character("char-1")]),
        sessions=_StubSessions(fails=True),
    )

    snapshot = await service.snapshot_for_operator(OPERATOR, now=NOW)

    assert snapshot.story_scenes_daily == QuotaUsage(limit=5, used=None)


async def test_one_broken_counter_does_not_take_the_others_down() -> None:
    service, _, _, _ = _service(
        _limited(),
        characters=_StubCharacters([_character("char-1")]),
        usage=_StubUsage(fails=True),
        sessions=_StubSessions(2),
    )

    snapshot = await service.snapshot_for_operator(OPERATOR, now=NOW)

    assert snapshot.character_slots == QuotaUsage(limit=3, used=1)
    assert snapshot.character_daily_creates == QuotaUsage(limit=1, used=None)
    assert snapshot.story_scenes_daily == QuotaUsage(limit=5, used=2)


async def test_failure_is_logged_as_a_warning_not_raised(
    caplog: pytest.LogCaptureFixture,
) -> None:
    service, _, _, _ = _service(_limited(), usage=_StubUsage(fails=True))

    with caplog.at_level("WARNING"):
        await service.snapshot_for_operator(OPERATOR, now=NOW)

    assert any(r.levelname == "WARNING" for r in caplog.records)


# --- fail-soft: an unwired dependency degrades the same way ------------------


async def test_missing_character_repository_leaves_both_counts_unknown() -> None:
    service, _, _, _ = _service(_limited(), omit_characters=True)

    snapshot = await service.snapshot_for_operator(OPERATOR, now=NOW)

    assert snapshot.character_slots == QuotaUsage(limit=3, used=None)
    assert snapshot.story_scenes_daily == QuotaUsage(limit=5, used=None)


async def test_missing_usage_repository_leaves_create_count_unknown() -> None:
    service, _, _, _ = _service(_limited(), omit_usage=True)

    snapshot = await service.snapshot_for_operator(OPERATOR, now=NOW)

    assert snapshot.character_daily_creates == QuotaUsage(limit=1, used=None)


async def test_missing_session_repository_leaves_scene_count_unknown() -> None:
    service, _, _, _ = _service(
        _limited(),
        characters=_StubCharacters([_character("char-1")]),
        omit_sessions=True,
    )

    snapshot = await service.snapshot_for_operator(OPERATOR, now=NOW)

    assert snapshot.story_scenes_daily == QuotaUsage(limit=5, used=None)


# --- clock ------------------------------------------------------------------


async def test_clock_supplies_now_when_the_caller_does_not() -> None:
    service, _, usage, _ = _service(_limited(), clock=_StubClock(NOW))

    await service.snapshot_for_operator(OPERATOR)

    assert usage is not None
    assert usage.calls[0]["since"] == NOW - CHARACTER_CREATE_LIMIT_WINDOW


async def test_naive_now_is_normalized_to_utc() -> None:
    service, _, usage, _ = _service(_limited())

    await service.snapshot_for_operator(
        OPERATOR, now=datetime(2026, 6, 1, 12, 0),
    )

    assert usage is not None
    assert usage.calls[0]["since"] == NOW - CHARACTER_CREATE_LIMIT_WINDOW


async def test_without_a_clock_the_real_utc_now_is_used() -> None:
    service, _, usage, _ = _service(_limited())

    before = datetime.now(timezone.utc)
    await service.snapshot_for_operator(OPERATOR)
    after = datetime.now(timezone.utc)

    assert usage is not None
    since = usage.calls[0]["since"]
    assert isinstance(since, datetime)
    assert (
        before - CHARACTER_CREATE_LIMIT_WINDOW
        <= since
        <= after - CHARACTER_CREATE_LIMIT_WINDOW
    )


async def test_snapshot_is_read_only_and_never_writes() -> None:
    """The stubs expose only reads; the service must not reach for anything
    else (``record_event``, ``add``, …) — this surface is display-only."""
    service, _, usage, sessions = _service(
        _limited(), characters=_StubCharacters([_character("char-1")]),
    )

    await service.snapshot_for_operator(OPERATOR, now=NOW)

    assert usage is not None and not hasattr(usage, "record_event")
    assert sessions is not None and not hasattr(sessions, "add")


# --- the real seams: service wired to the real in-memory repositories --------
#
# Every test above talks to hand-written stubs, and a stub cannot notice a
# port signature drifting: ``_read`` swallows the resulting ``TypeError``
# into ``used=None``, so a renamed keyword on any of the three ports would
# leave all the stub tests green while production silently reports every
# usage as unknown. This one wires the real in-memory implementations and
# asserts *numbers*, turning that silent degradation back into a red test.


async def test_snapshot_counts_through_the_real_in_memory_repositories() -> None:
    characters = InMemoryCharacterRepository()
    await characters.save(_character("char-1"))
    await characters.save(_character("char-2"))
    # Someone else's character must count into neither the slots nor the
    # scene roster.
    await characters.save(_character("char-x", user_id="someone-else"))

    usage = InMemoryAccountRuntimeUsageRepository()
    await usage.record_event(
        operator_id=OPERATOR,
        event_type=ACCOUNT_RUNTIME_EVENT_CHARACTER_CREATE,
        occurred_at=NOW - timedelta(hours=1),
        resource_id="char-2",
    )
    # Outside the rolling window — must not count.
    await usage.record_event(
        operator_id=OPERATOR,
        event_type=ACCOUNT_RUNTIME_EVENT_CHARACTER_CREATE,
        occurred_at=NOW - CHARACTER_CREATE_LIMIT_WINDOW - timedelta(hours=1),
        resource_id="char-1",
    )

    sessions = InMemoryStorySceneSessionRepository()
    await sessions.add(StorySceneSession(
        id="scene-in-window",
        character_id="char-1",
        conversation_id="conv-1",
        source_layer=SCENE_LAYER_SIDE_STORY,
        opened_at=NOW - timedelta(hours=2),
    ))
    # Outside the rolling window — must not count.
    await sessions.add(StorySceneSession(
        id="scene-out-of-window",
        character_id="char-2",
        conversation_id="conv-2",
        source_layer=SCENE_LAYER_SIDE_STORY,
        opened_at=NOW - STORY_SCENE_DAILY_LIMIT_WINDOW - timedelta(hours=1),
    ))

    service = PlayerRuntimeLimitsService(
        profiles=_StubProfiles(_limited()),
        characters=characters,
        usage_repository=usage,
        story_scene_sessions=sessions,
    )

    snapshot = await service.snapshot_for_operator(OPERATOR, now=NOW)

    assert snapshot.character_slots == QuotaUsage(limit=3, used=2)
    assert snapshot.character_daily_creates == QuotaUsage(limit=1, used=1)
    assert snapshot.story_scenes_daily == QuotaUsage(limit=5, used=1)
