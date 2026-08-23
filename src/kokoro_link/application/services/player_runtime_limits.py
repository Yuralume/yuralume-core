"""Display-only view of the hosted account's runtime ceilings.

Every number here is already enforced somewhere else — ``max_characters`` and
``daily_character_create_limit`` in
:mod:`~kokoro_link.application.services.character_service`,
``story_scene_daily_limit`` in
:mod:`~kokoro_link.application.services.story_scene_quota`,
``max_messages_per_session`` and the three capability flags in their own
services. Hosted players met all of them the same way: press the button, get
an error. This service exists so the SPA can say it *before* the press.

**It is a mirror, never a gate.** It performs no write of any kind (not even
the ledger claims the guards make), and no caller may use its answer as
permission: the server-side guard is still the only thing that decides.

**Fail-soft, on purpose in the opposite direction to every guard it
mirrors.** The guards fail *closed* — an unreadable ledger denies, because a
tier that configured a ceiling must not silently get an unbounded one. Here
an unreadable counter yields ``used=None`` ("we don't know how many you have
used"), the ``limit`` is still reported, and nothing raises. Taking the
guards' direction here would mean a transient DB hiccup blanks the whole hint
surface, or worse, tells a player they are out of slots when they are not.
The safe direction for a hint is to fall silent about the part it cannot
verify, and the frontend renders "unknown" rather than a blocking state (see
``frontend/src/composables/useActionPricing.ts`` for the same red line).

A ``None`` limit is the single spelling of "unlimited" for every quota here,
matching :class:`~kokoro_link.domain.value_objects.account_runtime_profile.AccountRuntimeProfile`.
An unlimited quota short-circuits before its dependency is ever touched, so
self-host (which never receives a restrictive profile) pays nothing for this
module beyond one profile resolution.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable, Protocol, Sequence, TypeVar

from kokoro_link.application.services.character_service import (
    CHARACTER_CREATE_LIMIT_WINDOW,
)
from kokoro_link.application.services.story_scene_quota import (
    STORY_SCENE_DAILY_LIMIT_WINDOW,
)
from kokoro_link.contracts.account_runtime_profile import (
    AccountRuntimeProfileResolverPort,
)
from kokoro_link.contracts.account_runtime_usage import (
    ACCOUNT_RUNTIME_EVENT_CHARACTER_CREATE,
    AccountRuntimeUsageRepositoryPort,
)
from kokoro_link.contracts.clock import ClockPort, ensure_utc
from kokoro_link.contracts.story_scene import StorySceneSessionRepositoryPort
from kokoro_link.domain.entities.character import Character

_LOGGER = logging.getLogger(__name__)

_Dependency = TypeVar("_Dependency")
_Result = TypeVar("_Result")

class _CharacterLister(Protocol):
    """The one method this service needs from ``CharacterRepositoryPort``.

    Structural for the same reason
    :class:`~kokoro_link.application.services.story_scene_quota.StorySceneQuotaGuard`
    does it: depending on the whole repository surface here would drag every
    other method it happens to have into this module's tests.
    """

    async def list_for_user(self, user_id: str) -> list[Character]: ...


@dataclass(frozen=True, slots=True)
class QuotaUsage:
    """One ceiling and how much of it is spent.

    ``used`` is ``None`` when the counter could not be read (or its
    repository is not wired). The ``limit`` is still reported in that case —
    "3 slots, spent unknown" is a strictly more useful hint than nothing, and
    it is honest about which half is missing.
    """

    limit: int
    used: int | None


@dataclass(frozen=True, slots=True)
class BackgroundPolicySnapshot:
    """The hosted account's background-scheduling policy, for display only.

    Unlike every quota above it, this is not a ceiling the player can spend
    against — it is the shape of what happens when they *stop* playing, which
    the SPA needs in order to say so before it happens rather than leaving the
    player to notice their character went quiet.
    """

    dormancy_days: int | None
    """``background_dormancy_days`` — days without a foreground interaction
    after which this account's characters stop scheduling background work
    (NF4). ``None`` = never (self-host, and any tier that has not set it)."""


@dataclass(frozen=True, slots=True)
class PlayerRuntimeLimitsSnapshot:
    """What the SPA needs to show a limit before the player hits it.

    Each quota is ``None`` when the tier does not cap it — deliberately the
    same "``None`` means unlimited" convention as the profile, so no consumer
    has to learn a second one.
    """

    character_slots: QuotaUsage | None
    character_daily_creates: QuotaUsage | None
    story_scenes_daily: QuotaUsage | None
    session_message_limit: int | None
    album_generation_enabled: bool
    tts_enabled: bool
    video_generation_enabled: bool
    background: BackgroundPolicySnapshot = BackgroundPolicySnapshot(
        dormancy_days=None,
    )
    """Additive (NF4), defaulted so no existing constructor call changes. The
    default is the permissive one for the same reason every quota's is: a
    caller that does not know about dormancy must not accidentally assert it."""


class PlayerRuntimeLimitsService:
    """Assemble a :class:`PlayerRuntimeLimitsSnapshot` for one operator.

    Every collaborator except the profile resolver is optional: a container
    that has not wired one (or a caller that only wants the flags) gets
    ``used=None`` for the affected quota rather than an exception, which is
    the same degradation an unreadable repository produces. There is exactly
    one behaviour for "cannot count", however it came about.
    """

    def __init__(
        self,
        *,
        profiles: AccountRuntimeProfileResolverPort,
        characters: _CharacterLister | None = None,
        usage_repository: AccountRuntimeUsageRepositoryPort | None = None,
        story_scene_sessions: StorySceneSessionRepositoryPort | None = None,
        clock: ClockPort | None = None,
    ) -> None:
        self._profiles = profiles
        self._characters = characters
        self._usage = usage_repository
        self._sessions = story_scene_sessions
        self._clock = clock

    async def snapshot_for_operator(
        self, operator_id: str, *, now: datetime | None = None,
    ) -> PlayerRuntimeLimitsSnapshot:
        """The operator's ceilings and current usage. Never writes anything."""
        profile = await self._profiles.resolve_for_operator(operator_id)
        moment = ensure_utc(now) if now is not None else self._now()
        # Resolved once and shared: the slot count and the scene count both
        # need the roster, and asking twice would double the cost of the most
        # expensive read on this path (and could report two different numbers
        # for the same player in one response).
        slot_limit = profile.max_characters
        create_limit = profile.daily_character_create_limit
        scene_limit = profile.story_scene_daily_limit
        roster = await self._roster(
            operator_id,
            needed=(slot_limit is not None or scene_limit is not None),
        )
        creates_used = await self._count_character_creates(
            operator_id, moment, needed=create_limit is not None,
        )
        scenes_used = await self._count_story_scenes(
            operator_id, roster, moment, needed=scene_limit is not None,
        )
        return PlayerRuntimeLimitsSnapshot(
            character_slots=_quota(
                slot_limit, None if roster is None else len(roster),
            ),
            character_daily_creates=_quota(create_limit, creates_used),
            story_scenes_daily=_quota(scene_limit, scenes_used),
            session_message_limit=profile.max_messages_per_session,
            album_generation_enabled=profile.album_generation_enabled,
            tts_enabled=profile.tts_enabled,
            video_generation_enabled=profile.video_generation_enabled,
            # Read straight off the profile: unlike the quotas there is no
            # counter to pair it with, so there is nothing here that can fail
            # softly — either the profile resolved or the whole snapshot did
            # not.
            background=BackgroundPolicySnapshot(
                dormancy_days=profile.background_dormancy_days,
            ),
        )

    async def _roster(
        self, operator_id: str, *, needed: bool,
    ) -> Sequence[Character] | None:
        """Every character this operator owns, or ``None`` when unknowable."""
        if not needed:
            return None
        return await self._read(
            "character roster",
            operator_id,
            self._characters,
            lambda repo: repo.list_for_user(operator_id),
        )

    async def _count_character_creates(
        self, operator_id: str, moment: datetime, *, needed: bool,
    ) -> int | None:
        if not needed:
            return None
        return await self._read(
            "character create ledger",
            operator_id,
            self._usage,
            lambda repo: repo.count_events(
                operator_id=operator_id,
                event_type=ACCOUNT_RUNTIME_EVENT_CHARACTER_CREATE,
                since=moment - CHARACTER_CREATE_LIMIT_WINDOW,
                until=moment,
            ),
        )

    async def _count_story_scenes(
        self,
        operator_id: str,
        roster: Sequence[Character] | None,
        moment: datetime,
        *,
        needed: bool,
    ) -> int | None:
        """Openings across *all* of the operator's characters (SC3-B is a
        per-player ceiling, not a per-character one).

        An unknown roster yields an unknown count rather than a count over a
        narrowed set: quoting ``0/5`` to a player who has actually spent four
        openings is worse than admitting the number is unavailable.
        """
        if not needed or roster is None:
            return None
        character_ids = tuple({c.id for c in roster if getattr(c, "id", None)})
        if not character_ids:
            # A player with no characters cannot have opened a scene. Skipping
            # the query also avoids handing an empty ``IN ()`` to the backend.
            return 0
        return await self._read(
            "story scene sessions",
            operator_id,
            self._sessions,
            lambda repo: repo.count_opened_since(
                character_ids,
                since=moment - STORY_SCENE_DAILY_LIMIT_WINDOW,
            ),
        )

    async def _read(
        self,
        what: str,
        operator_id: str,
        dependency: _Dependency | None,
        load: Callable[[_Dependency], Awaitable[_Result]],
    ) -> _Result | None:
        """Run one counter read; degrade to ``None`` on absence or failure.

        The two ways a count can be unavailable — the repository was never
        wired, or reading it blew up — collapse here into the one outcome the
        contract has for both, so no caller has to branch on the difference.
        """
        if dependency is None:
            _LOGGER.warning(
                "player runtime limits: %s is not wired (operator=%s); "
                "reporting usage as unknown",
                what, operator_id,
            )
            return None
        try:
            return await load(dependency)
        except Exception:  # noqa: BLE001 - a hint must never break the page
            _LOGGER.warning(
                "player runtime limits: %s read failed (operator=%s); "
                "reporting usage as unknown",
                what, operator_id, exc_info=True,
            )
            return None

    def _now(self) -> datetime:
        if self._clock is not None:
            return ensure_utc(self._clock.now())
        return datetime.now(timezone.utc)


def _quota(limit: int | None, used: int | None) -> QuotaUsage | None:
    """``None`` limit means unlimited, and unlimited has no usage to show."""
    if limit is None:
        return None
    return QuotaUsage(limit=limit, used=used)
