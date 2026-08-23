"""The foreground-interaction anchor, for every surface that is one (NF4).

``CharacterState.last_active_at`` is the single fact behind every "is this
player still here" question in the system: NF4 dormancy ("has the player ever
engaged this character, and how long ago"), the B1 idle down-shift, the freeze
reaper's last-interaction cut-off, and the feed's silence anchor.

It was written in exactly one place — the end of a chat turn. That was true
enough when chat was the only way to play, and quietly wrong afterwards: a
player can now spend credits every day inside 分歧劇場 or press 起幕 without
ever opening the chat, and every one of those days left the anchor exactly
where it was. Under a tier with ``background_dormancy_days`` that reads as
"never interacted" ⇒ permanently dormant ⇒ a character the player is paying
to play with has no background life at all.

So the anchor gets its own tiny collaborator, and every paid foreground entry
point calls it. Two properties matter and both live in the repository's
``touch_last_active``:

* **targeted** — a single-column UPDATE, not an aggregate ``save()``. These
  callers hold entities they loaded *before* a long model call; writing the
  whole aggregate back would clobber whatever a concurrent chat turn wrote in
  the meantime.
* **monotonic** — a slow action that started before a chat turn cannot drag
  the anchor backwards.

Self-host neutral by construction — and the construction is the *wiring*, not
the knob. ``last_active_at`` is not a dormancy-only field: on self-host it is
also the "has this player ever opened their mouth" gate in front of proactive
messaging (``_has_user_started_interaction``), the idle-minutes input, the
feed's silence anchor and the runtime activity gate. Writing it from a surface
that never used to write it would give a self-host player who only plays 分歧
劇場 proactive messages they never used to get — with the dormancy knob still
NULL. So ``bootstrap/container.py`` builds this collaborator **in cloud mode
only**; self-host passes ``None`` to all three services and they keep their
pre-NF4 path exactly. Nothing here has to check a mode: an anchor that was
never built cannot move anything.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import datetime, timezone

from kokoro_link.contracts.clock import ClockPort, ensure_utc
from kokoro_link.contracts.repositories import CharacterRepositoryPort
from kokoro_link.domain.entities.character import Character

_LOGGER = logging.getLogger(__name__)


class CharacterActivityAnchor:
    """Advances ``last_active_at`` for paid foreground interactions.

    Fail-soft on purpose: the interaction the player paid for has already
    happened by the time this runs, so a bookkeeping write that fails must be
    logged and swallowed, never turned into a failed action (which — for the
    charged surfaces that call this — would also mean a refund for work that
    was actually delivered). The cost of a lost touch is one missed anchor
    advance, corrected by the next interaction.
    """

    __slots__ = ("_characters", "_clock")

    def __init__(
        self,
        character_repository: CharacterRepositoryPort,
        *,
        clock: ClockPort | None = None,
    ) -> None:
        self._characters = character_repository
        self._clock = clock

    async def touch(
        self, character: Character | str, *, now: datetime | None = None,
    ) -> None:
        """Mark one character as engaged in the foreground, right now."""
        character_id = (
            character if isinstance(character, str) else character.id
        )
        if not character_id:
            return
        moment = self._resolve_now(now)
        try:
            await self._characters.touch_last_active(character_id, moment)
        except Exception:
            _LOGGER.exception(
                "activity anchor: touch failed character=%s", character_id,
            )

    async def touch_all(
        self,
        characters: Iterable[Character | str],
        *,
        now: datetime | None = None,
    ) -> None:
        """Mark a whole cast as engaged (a 分歧劇場 press plays all of them).

        One instant for the whole cast so the members of one press cannot end
        up with anchors minutes apart; each is touched independently so one
        failure does not cost the others theirs.
        """
        moment = self._resolve_now(now)
        for character in characters:
            await self.touch(character, now=moment)

    def _resolve_now(self, now: datetime | None) -> datetime:
        if now is not None:
            return ensure_utc(now)
        if self._clock is not None:
            return ensure_utc(self._clock.now())
        return datetime.now(timezone.utc)
