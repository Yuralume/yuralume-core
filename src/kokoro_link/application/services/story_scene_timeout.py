"""Idle 起幕 scenes wrap themselves up (SC1-E).

A player can walk away mid-scene, and the design says what
happens then: after a configurable idle window (24h by default) the scene
closes on its own, the wrap-up writer narrates 「你離開之後…」, and the
beat lands as canon. Two promises depend on it — the hosted one price
already paid for the opening must produce something (§3.4 #2), and a beat
staged into a scene nobody finished must not sit in limbo forever.

**Nothing about the close lives here.** This module decides *which*
sessions are due and *who* to run them for; the close itself is
``StorySceneService.close_scene(mode=timeout)``, the same call the
player's 「結束場景」 button makes. The red line that a wrap-up must never
invent player actions is enforced inside that shared core, and the timeout
path — the one nobody is watching — is precisely the path a second
implementation would drift on.

Two entry points, one closing helper:

* :meth:`StorySceneTimeoutCloser.close_if_idle` is the per-character step
  both schedulers run (the embedded tick body and the distributed
  ``story_scene_timeout`` chain). It costs one indexed read for a
  character who is not in a scene, which is almost always.
* :meth:`StorySceneTimeoutCloser.sweep_unowned` is the backstop for rows
  **no chain covers**: a session whose character row is gone. SQLite does
  not enforce the ``ON DELETE CASCADE`` the table declares, so on a
  self-host install deleting a character genuinely strands its open scene.
  Those rows are closed straight through the repository — no writer, no
  canon: there is no character left to remember it, and asking a wrap-up
  writer to narrate for a deleted character is how you get a memory
  attached to nothing.

Failure direction throughout: **leave it for the next tick.** The sweep is
periodic and the close is a compare-and-set, so retrying costs nothing and
losing a pass costs one cadence of latency; raising into a scheduler tick
would cost every step after this one.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Protocol

from kokoro_link.application.services.chat_turn_lease import (
    ConversationBusyError,
)
from kokoro_link.contracts.clock import ensure_utc
from kokoro_link.contracts.story_scene import (
    StorySceneSessionRepositoryPort,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.story_scene_session import (
    SCENE_CLOSE_TIMEOUT,
    StorySceneSession,
)

_LOGGER = logging.getLogger(__name__)


DEFAULT_STORY_SCENE_IDLE_TIMEOUT_SECONDS = 24 * 60 * 60.0
"""How long a scene may sit untouched before it closes itself (§2 #5).

24 hours is the plan's own default and the only number in this feature's
pacing that lives in code — §3.4 #3 bans hard-coded rhythm, and this is
not rhythm: it is the outer bound on how long a paid-for opening may
remain unresolved. Overridable per deployment
(``YURALUME_STORY_SCENE_IDLE_TIMEOUT_SECONDS``); a self-host operator who
plays in long, slow sessions can widen it without touching code.
"""

DEFAULT_UNOWNED_SWEEP_LIMIT = 50
"""Rows one unowned-sweep pass will look at.

The pass costs one character lookup per stale row, so it is bounded rather
than unbounded; oldest-first ordering means a backlog drains in order
instead of the same head being re-read forever."""


class _SceneCloserView(Protocol):
    """The one method this module needs from ``StorySceneService``.

    Structural on purpose: depending on the whole scene service here would
    drag the opener, the waterfall and the quota guard into every test of
    a timeout sweep."""

    async def close_scene(
        self,
        character: Character,
        *,
        session: StorySceneSession,
        mode: str,
        now: datetime | None = None,
    ): ...


class _CharacterReader(Protocol):
    """The one method this module needs from ``CharacterRepositoryPort``."""

    async def get(self, character_id: str) -> Character | None: ...


class StorySceneTimeoutCloser:
    """Closes 起幕 scenes that have gone idle."""

    def __init__(
        self,
        *,
        sessions: StorySceneSessionRepositoryPort,
        scenes: _SceneCloserView,
        characters: _CharacterReader,
        idle_timeout_seconds: float = DEFAULT_STORY_SCENE_IDLE_TIMEOUT_SECONDS,
        unowned_sweep_limit: int = DEFAULT_UNOWNED_SWEEP_LIMIT,
    ) -> None:
        self._sessions = sessions
        self._scenes = scenes
        self._characters = characters
        # A non-positive window would close every scene the instant it
        # opened — including the one the player is reading right now — so
        # it is clamped rather than trusted. One minute is the smallest
        # value that is still a *window*; anything smaller is a mistake.
        self._idle_timeout = timedelta(
            seconds=max(60.0, float(idle_timeout_seconds)),
        )
        self._unowned_sweep_limit = max(0, int(unowned_sweep_limit))

    @property
    def idle_timeout(self) -> timedelta:
        """The configured idle window. One source for every reader."""
        return self._idle_timeout

    # ── per character (both scheduling lines) ────────────────────────

    async def close_if_idle(
        self, character: Character, *, now: datetime | None = None,
    ) -> bool:
        """Wrap this character's scene up if it has gone quiet.

        ``False`` — the usual answer — means there was nothing to do: no
        live scene, or one the player is still inside. Never raises; a
        scheduler step that cannot read a session must not take the rest
        of the tick down with it.
        """
        moment = _as_utc(now)
        try:
            session = await self._sessions.get_open_for_character(character.id)
        except Exception:  # noqa: BLE001 - retried next tick
            _LOGGER.exception(
                "story scene timeout: live-scene lookup failed character=%s",
                character.id,
            )
            return False
        if session is None or not self.is_idle(session, now=moment):
            return False
        return await self._close(session, character=character, now=moment)

    def is_idle(
        self, session: StorySceneSession, *, now: datetime | None = None,
    ) -> bool:
        """Has ``session`` been untouched for longer than the window?"""
        return ensure_utc(session.last_activity_at) + self._idle_timeout <= (
            _as_utc(now)
        )

    async def next_timeout_at(
        self, character: Character, now: datetime | None = None,
    ) -> datetime | None:
        """When this character's live scene becomes due, if it has one.

        The distributed chain's ``explicit_next_due`` hook: a live scene's
        deadline is known exactly (``last_activity_at`` + window), so the
        chain jumps straight to it instead of rechecking on the fallback
        cadence for up to an hour past the window.

        ``None`` — no live scene, an unreadable session, or a deadline that
        has **already passed** — leaves the chain on its base interval.
        The past-deadline case is the subtle one: it can only mean the
        close this same job just attempted did not happen (the player was
        mid-turn), and an elapsed instant says nothing about when to try
        again. Answering with it anyway would mint a next-due inside the
        window the current occurrence already owns, which dedups against
        that occurrence's own idempotency key and breaks the chain until
        the reconciler notices — a far worse outcome than one interval of
        latency on a scene whose player is demonstrably back.
        """
        moment = _as_utc(now)
        try:
            session = await self._sessions.get_open_for_character(character.id)
        except Exception:  # noqa: BLE001 - fall back to the base cadence
            _LOGGER.exception(
                "story scene timeout: due lookup failed character=%s",
                character.id,
            )
            return None
        if session is None:
            return None
        deadline = ensure_utc(session.last_activity_at) + self._idle_timeout
        return deadline if deadline > moment else None

    # ── unowned rows (self-host backstop) ────────────────────────────

    async def sweep_unowned(
        self, *, now: datetime | None = None, limit: int | None = None,
    ) -> int:
        """Close stale sessions whose character no longer exists.

        Sessions that still have a character are left alone on purpose:
        their own chain closes them through the wrap-up writer, and doing
        it here as well would spend a second LLM call racing the first.
        Returns how many rows this pass closed.
        """
        moment = _as_utc(now)
        bound = self._unowned_sweep_limit if limit is None else max(0, limit)
        if bound == 0:
            return 0
        try:
            stale = await self._sessions.list_stale_open(
                idle_before=moment - self._idle_timeout, limit=bound,
            )
        except Exception:  # noqa: BLE001 - retried next sweep
            _LOGGER.exception("story scene timeout: stale-session scan failed")
            return 0
        closed = 0
        for session in stale:
            owner, readable = await self._character_of(session)
            if owner is not None or not readable:
                continue
            if await self._close_unowned(session, now=moment):
                closed += 1
        return closed

    # ── steps ────────────────────────────────────────────────────────

    async def _close(
        self,
        session: StorySceneSession,
        *,
        character: Character | None,
        now: datetime,
    ) -> bool:
        resolved = character
        if resolved is None:
            resolved, readable = await self._character_of(session)
            if not readable:
                # A failed read is NOT a deleted character: mistaking the
                # two would retire live scenes without narrating them
                # every time the character table hiccuped.
                return False
            if resolved is None:
                return await self._close_unowned(session, now=now)
        try:
            closing = await self._scenes.close_scene(
                resolved,
                session=session,
                mode=SCENE_CLOSE_TIMEOUT,
                now=now,
            )
        except ConversationBusyError:
            # The player came back and is mid-turn — which is the best
            # possible reason not to end their scene. Not an error: the
            # next pass either finds the scene alive again (the turn bumped
            # last_activity_at) or closes it then.
            _LOGGER.info(
                "story scene timeout: scene busy, retrying next pass "
                "scene=%s character=%s",
                session.id,
                session.character_id,
            )
            return False
        except Exception:  # noqa: BLE001 - retried next pass
            _LOGGER.exception(
                "story scene timeout close failed scene=%s character=%s",
                session.id,
                session.character_id,
            )
            return False
        if closing.already_closed:
            # Someone else wrapped it up between the scan and the close.
            # An ordinary race with an equally valid winner.
            return False
        _LOGGER.info(
            "story scene closed on idle timeout scene=%s character=%s "
            "layer=%s beat=%s",
            session.id,
            session.character_id,
            session.source_layer,
            session.beat_id,
        )
        return True

    async def _close_unowned(
        self, session: StorySceneSession, *, now: datetime,
    ) -> bool:
        """Retire a row whose character is gone — silently, and as itself.

        No wrap-up writer and no canon: canon is something a character
        remembers, and this one no longer exists. Straight through the
        repository's compare-and-set so the row still ends up carrying
        ``closed_reason='timeout'`` like every other timed-out scene."""
        try:
            closed = await self._sessions.close(
                session.id, reason=SCENE_CLOSE_TIMEOUT, at=now,
            )
        except Exception:  # noqa: BLE001 - retried next sweep
            _LOGGER.exception(
                "story scene timeout: unowned close failed scene=%s",
                session.id,
            )
            return False
        if closed is None:
            return False
        _LOGGER.info(
            "story scene retired: character no longer exists scene=%s "
            "character=%s",
            session.id,
            session.character_id,
        )
        return True

    async def _character_of(
        self, session: StorySceneSession,
    ) -> tuple[Character | None, bool]:
        """``(character, readable)``.

        Three answers, not two: a character, ``(None, True)`` for one that
        is genuinely gone, and ``(None, False)`` for one that could not be
        read. Only the middle case may retire a row — collapsing the other
        two would let a flaky character query silently retire live scenes.
        """
        try:
            return await self._characters.get(session.character_id), True
        except Exception:  # noqa: BLE001 - treat as "cannot decide yet"
            _LOGGER.exception(
                "story scene timeout: character lookup failed character=%s",
                session.character_id,
            )
            return None, False


def _as_utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    return ensure_utc(value)
