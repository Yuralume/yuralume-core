"""Per-tier daily ceiling on 起幕 (story scene) openings (SC3-B).

STORY_SCENE_PLAN §2 #3 is deliberately "no hard cap in code, only a
control-plane knob": hosted play already self-limits through per-action
螢火 spend (§3.4 red line 2), so ``story_scene_daily_limit`` exists purely as
an optional pacing guard rail an operator may dial in — the default (``None``)
is unlimited, and self-host installs never see anything else because the
knob is never sent to them.

**Window semantics.** The ceiling is a rolling 24h window
(:data:`STORY_SCENE_DAILY_LIMIT_WINDOW`), not an owner-local civil day. This
matches every other daily knob already in
:mod:`kokoro_link.domain.value_objects.account_runtime_profile` —
``daily_chat_image_limit`` / ``daily_feed_post_limit`` (see
``chat_service.py`` / ``feed_composer_service.py``, both ``since=now -
timedelta(hours=24)``) and ``daily_overage_limit``
(``quota_overage_service.OVERAGE_WINDOW``) — so a player who has learned what
"N per day" means from one of those does not get a different meaning here.

**What counts.** A scene counts the moment it opens, whether it is later
played to a close or abandoned (STORY_SCENE_PLAN §4.1 SC3-B: "按下去成功開場
就算一次，玩家自己棄置不退還額度") — the count comes straight from
``StorySceneSessionRepositoryPort.count_opened_since``, which does not filter
by status, so there is nothing here that could double- or under-count a
closed row.

**Scope.** The ceiling is per *player* (operator), not per character: an
operator with several characters shares one daily budget across all of them
(STORY_SCENE_PLAN §4.1 SC3-B), so :meth:`StorySceneQuotaGuard.check` looks up
every character the calling character's operator owns before counting.

**Fail-closed on infrastructure failure.** If the operator's character list
or the session count cannot be read, the guard denies
(:class:`StorySceneQuotaUnavailable`) rather than silently letting an
unbounded number of openings through — the same direction ``chat_service``'s
``daily_chat_image_limit`` and ``feed_composer_service``'s
``daily_feed_post_limit`` walls already fail in when their ledger is
unreadable. This branch is unreachable for the common case: a ``None``
limit (self-host, and every hosted tier until an operator opts in) returns
before either dependency is ever touched, so a broken guard cannot affect an
account that never configured one.

Deliberately independent of ``story_scene_service.py``: this module is a
standalone unit any caller can construct and test without pulling in the
whole scene-opening service. Wiring it into ``open_scene`` (and mapping its
exceptions to HTTP) is left to that service's own ticket.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Protocol, Sequence

from kokoro_link.contracts.account_runtime_profile import (
    AccountRuntimeProfileResolverPort,
)
from kokoro_link.contracts.clock import ensure_utc
from kokoro_link.contracts.story_scene import StorySceneSessionRepositoryPort
from kokoro_link.domain.entities.character import Character

_LOGGER = logging.getLogger(__name__)

STORY_SCENE_DAILY_LIMIT_WINDOW = timedelta(hours=24)
"""Rolling window for the ceiling — see the module docstring for why this
matches ``daily_chat_image_limit`` / ``daily_feed_post_limit`` /
``daily_overage_limit`` rather than an owner-local civil day."""


class _CharacterLister(Protocol):
    """The one method the guard needs from ``CharacterRepositoryPort``.

    Structural on purpose: the real collaborator is the character
    repository, but depending on its whole surface here would make this
    module's tests carry every other method that port happens to have.
    """

    async def list_for_user(self, user_id: str) -> list[Character]: ...


class StorySceneQuotaError(Exception):
    """Base for structured SC3-B quota-guard failures. ``code`` reaches the
    client once a caller maps it to an HTTP status."""

    code = "story_scene_quota_error"


class StorySceneDailyLimitReached(StorySceneQuotaError):
    """The operator has opened ``limit`` (or more) scenes in the window.

    Suggested mapping for the route that wires this in: **429** (the
    ceiling is a rate limit on a paid action, not a permission or validation
    failure, and it is retryable — tomorrow, or whenever the rolling window
    clears).
    """

    code = "story_scene_daily_limit_reached"

    def __init__(self, *, limit: int, used: int) -> None:
        super().__init__(
            f"story scene daily limit reached ({used}/{limit} in the last "
            f"{int(STORY_SCENE_DAILY_LIMIT_WINDOW.total_seconds() // 3600)}h)",
        )
        self.limit = limit
        self.used = used


class StorySceneQuotaUnavailable(StorySceneQuotaError):
    """The ceiling could not be enforced because a dependency failed.

    Fail-closed like the sibling ``daily_chat_image_limit`` /
    ``daily_feed_post_limit`` walls (see the module docstring): a hosted
    tier that configured a limit must not silently get an unbounded one just
    because a query broke. Suggested mapping: **503** (transient,
    retryable) — distinct from :class:`StorySceneDailyLimitReached`'s 429 so
    a client can tell "you are out for today" from "try again in a moment".
    """

    code = "story_scene_quota_unavailable"


class StorySceneQuotaGuard:
    """Enforces ``AccountRuntimeProfile.story_scene_daily_limit`` for 起幕.

    Not wired into :class:`~kokoro_link.application.services.story_scene_service.StorySceneService`
    by this module — see the module docstring's "Wiring" note. The intended
    call site is the top of ``StorySceneService.open_scene``, alongside
    (after) ``_ensure_account_may_open``: both are pre-checks that must run
    before any material is resolved or anything is written, and both raise
    rather than returning a sentinel so the route can map each exception to
    its own status code the way ``SceneAlreadyOpen`` /
    ``SceneMaterialUnavailable`` already do.
    """

    def __init__(
        self,
        *,
        sessions: StorySceneSessionRepositoryPort,
        characters: _CharacterLister,
        profiles: AccountRuntimeProfileResolverPort,
    ) -> None:
        self._sessions = sessions
        self._characters = characters
        self._profiles = profiles

    async def check(
        self, character: Character, *, now: datetime | None = None,
    ) -> None:
        """Raise when this character's operator has used up today's 起幕
        openings. Returns silently (the common case) when the tier's limit
        is ``None`` — self-host, and every hosted tier until an operator
        opts in — without ever touching the session repository or the
        character list.
        """
        operator_id = getattr(character, "user_id", None) or "default"
        profile = await self._profiles.resolve_for_operator(operator_id)
        limit = getattr(profile, "story_scene_daily_limit", None)
        if limit is None:
            return
        moment = ensure_utc(now) if now is not None else _utcnow()
        character_ids = await self._operator_character_ids(
            operator_id, fallback=character.id,
        )
        try:
            used = await self._sessions.count_opened_since(
                character_ids, since=moment - STORY_SCENE_DAILY_LIMIT_WINDOW,
            )
        except Exception as exc:  # noqa: BLE001 - an unreadable ledger denies
            _LOGGER.exception(
                "story scene quota: session count failed (operator=%s)",
                operator_id,
            )
            raise StorySceneQuotaUnavailable(
                "story scene daily limit could not be checked",
            ) from exc
        if used >= limit:
            raise StorySceneDailyLimitReached(limit=limit, used=used)

    async def _operator_character_ids(
        self, operator_id: str, *, fallback: str,
    ) -> Sequence[str]:
        """Every character id this operator owns.

        Raises :class:`StorySceneQuotaUnavailable` when the roster cannot be
        read rather than falling back to just ``fallback`` (the character
        actually opening the scene): silently narrowing the count to one
        character would *undercount* an operator with several, which is the
        wrong direction for a guard whose whole job is not letting an
        over-quota operator through. Same fail-closed direction
        ``chat_service``'s ``daily_chat_image_limit`` and
        ``feed_composer_service``'s ``daily_feed_post_limit`` already take
        when their own ledger read fails.
        """
        try:
            owned = await self._characters.list_for_user(operator_id)
        except Exception as exc:  # noqa: BLE001 - an unreadable roster denies
            _LOGGER.exception(
                "story scene quota: character list failed (operator=%s)",
                operator_id,
            )
            raise StorySceneQuotaUnavailable(
                "story scene daily limit could not be checked "
                "(character roster unreadable)",
            ) from exc
        ids = {c.id for c in owned if getattr(c, "id", None)}
        ids.add(fallback)
        return tuple(ids)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)
