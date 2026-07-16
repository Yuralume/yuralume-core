from __future__ import annotations

from datetime import datetime
from typing import Protocol

from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.conversation import Conversation, Message


class CharacterRepositoryPort(Protocol):
    async def list(self) -> list[Character]:
        """List stored characters (unfiltered).

        Background services that operate per-character (proactive
        scheduler, world-event scheduler, dream tick) read this and
        get the ``user_id`` from each ``Character`` entity — they
        never need to know which user is "current" because they don't
        run inside a request."""

    async def list_active(self) -> list[Character]:
        """List only non-frozen characters (CHARACTER_FREEZE_PLAN).

        The background scheduler iterates this instead of :meth:`list`
        so a frozen character costs zero per-tick background work while
        keeping its persisted state and staying reachable by id / owner
        for foreground chat and admin surfaces."""

    async def set_frozen(
        self,
        character_id: str,
        *,
        frozen: bool,
        now: datetime,
        reason: str | None = None,
    ) -> bool:
        """Flip the site-level freeze flag for one character.

        Freezing stamps ``frozen_at=now`` and ``frozen_reason=reason``
        (normally ``idle`` / ``manual``; ``subscription_lapse`` is retained
        only for legacy compatibility); unfreezing clears both back
        to ``None`` (``reason`` is ignored when ``frozen=False``).
        Implemented as a targeted update so it never races the
        character's state-tracking ``save()``. Returns ``True`` when a
        row was updated."""

    async def set_subscription_locked(
        self, character_id: str, *, locked: bool,
    ) -> bool:
        """Update only the retryable tenant-lock projection.

        Generic character saves must never write this field. Returns True
        when the row exists."""

    async def list_for_user(self, user_id: str) -> list[Character]:
        """List characters owned by ``user_id`` only.

        Front-end ``GET /characters`` call site. When
        ``KOKORO_AUTH_ENABLED=false`` the dependency layer always
        passes ``DEFAULT_OPERATOR_ID`` so this is effectively the same
        as ``list()`` on a fresh install."""

    async def get(self, character_id: str) -> Character | None:
        """Fetch character by id."""

    async def save(self, character: Character) -> None:
        """Store character."""

    async def delete(self, character_id: str) -> bool:
        """Remove the character. Returns True when a row was removed."""


class ConversationRepositoryPort(Protocol):
    async def get(self, conversation_id: str) -> Conversation | None:
        """Fetch conversation by id."""

    async def save(self, conversation: Conversation) -> None:
        """Store conversation."""

    async def latest_for_character(
        self, character_id: str, *, source: str | None = "web",
    ) -> Conversation | None:
        """Return the most recently updated conversation for a character.

        ``source`` filters by origin: the default ``"web"`` keeps the
        web UI's "latest chat" panel from picking up Telegram / LINE
        threads. Pass ``None`` to ignore the filter (useful for admin
        tools that genuinely want the most recent activity across every
        channel).

        **Note**: this returns *one* per-channel conversation, so it's
        appropriate for surface-bound UI display only. For LLM context
        building (chat / proactive / schedule / feed) call
        ``recent_messages_for_character`` instead — the character is a
        single person across every channel and their prompt history
        must be a unified timeline.
        """

    async def recent_messages_for_character(
        self,
        character_id: str,
        *,
        limit: int,
        exclude_tool_only: bool = False,
    ) -> list[Message]:
        """Return the character's most recent ``limit`` messages **merged
        across every source** (web + telegram + line + …), sorted by
        ``Message.created_at`` ascending.

        Treats the character as one person with one timeline rather than
        a per-channel persona. ``exclude_tool_only`` drops messages
        whose only payload is a tool artifact (e.g. ``/pic`` images) so
        downstream summarisers / planners don't try to "respond" to a
        bare image URL.
        """

    async def has_user_message_for_character(self, character_id: str) -> bool:
        """Return whether any user-authored turn exists for this character.

        Used by proactive dispatch as a legacy-data fallback when
        ``CharacterState.last_active_at`` is missing. This is deliberately
        cross-source: one Telegram/LINE/Discord/web user message is enough
        to mark the relationship as started.
        """

    async def delete_for_character(self, character_id: str) -> int:
        """Cascade-delete all conversations (and their messages) for a character.

        Returns the number of conversations removed.
        """


class PreferencesRepositoryPort(Protocol):
    """Schema-less key/value store for global UI preferences.

    Values are Python primitives (``str | int | float | bool | None``
    or a ``dict``/``list`` of those). The adapter handles JSON
    (de)serialisation, callers never deal with strings-of-JSON.
    """

    async def get(self, key: str) -> object | None:
        """Return the stored value for ``key``, or ``None`` when absent."""

    async def set(self, key: str, value: object) -> None:
        """Store ``value`` under ``key`` (upsert)."""

    async def delete(self, key: str) -> bool:
        """Remove the key. Returns True when a row was removed."""
