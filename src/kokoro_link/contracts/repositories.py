from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.conversation import Conversation, Message


@dataclass(frozen=True, slots=True)
class AppendResult:
    """Outcome of a CAS append to a conversation (LH2 / DR-LH0-004).

    ``ok`` — the expected next position matched and the rows were inserted.
    ``conflict`` — another writer advanced the tail (a lost-update race); no
    rows were written. ``row_ids`` / ``positions`` carry the durable
    coordinates of the just-appended messages (empty on conflict), in the same
    order they were supplied.
    """

    ok: bool
    conflict: bool
    row_ids: tuple[int, ...] = ()
    positions: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class ConversationMessagePage:
    """One keyset page of a conversation's message tail (IV10).

    Deliberately **not** a :class:`Conversation`. A truncated snapshot handed
    to :meth:`ConversationRepositoryPort.save` would rewrite the whole row from
    a partial history and delete everything the page left out; making the page
    its own type means that mistake cannot be typed. Read paths that need the
    entire thread (prompt building, proactive delivery, story scenes) keep
    calling :meth:`ConversationRepositoryPort.latest_for_character`.

    ``messages`` is oldest-first *within the page*, matching the order a full
    conversation returns. ``next_before`` is the ``position`` of the page's
    oldest message — pass it back as ``before_position`` to fetch the page
    before it — and is ``None`` whenever ``has_more`` is false, so a client can
    short-circuit exactly like the feed's cursor does.
    """

    conversation_id: str
    character_id: str
    messages: tuple[Message, ...]
    has_more: bool
    next_before: int | None = None


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

    async def list_by_origin_official_card_id(
        self, card_id: str,
    ) -> list[Character]:
        """List every character installed from official card ``card_id``.

        Cross-tenant by design (EC10-B): a card's Cloud-side
        contract-termination freeze silences every character built from it
        site-wide, not just one operator's roster, so this is the one
        character lookup that is not scoped by owner. Self-host rows never
        match — ``origin_official_card_id`` is always ``None`` there."""

    async def claim_consolidation_slot(
        self, character_id: str, *, cooldown: timedelta,
    ) -> bool:
        """Atomically claim this character's next memory-consolidation run.

        One conditional UPDATE of ``last_consolidated_at`` fenced on the
        cooldown cutoff — it decides *cooldown* and *single-flight* together,
        across processes, in a statement the DB serialises. ``True`` means the
        caller owns this run; ``False`` means someone already claimed it
        (another replica right now, or anyone within the cooldown window).

        The claim is consumed on **attempt**, not on success: a failed
        consolidation does not roll it back, matching the historical behaviour
        of stamping the run timestamp before running so a crash loop can't
        re-enter every turn.

        Timing is the implementation's authority — the DB clock for SA-backed
        adapters, never the caller's wall clock, because replicas' clocks
        drift and a skewed replica must not shorten the shared cooldown."""

    async def get(self, character_id: str) -> Character | None:
        """Fetch character by id."""

    async def save(self, character: Character) -> None:
        """Store character.

        Never writes the dedicated control fields (``frozen*``,
        ``subscription_locked``, ``last_consolidated_at``) — those have their
        own targeted operations so an unrelated aggregate write cannot
        resurrect a consumed claim or clobber a lock."""

    async def touch_last_active(self, character_id: str, now: datetime) -> bool:
        """Advance ``CharacterState.last_active_at`` to ``now``. Monotonic.

        The foreground-interaction anchor every "is this player still here"
        question reads (dormancy, idle down-shift, freeze reaping, feed
        silence). Chat has always written it as part of its whole-aggregate
        turn save; every *other* paid foreground surface (起幕, 分歧劇場 …)
        holds an entity that was loaded before a long model call, so a
        targeted single-column UPDATE is the only honest way for them to move
        it: an aggregate ``save()`` from those paths would write back state
        that a concurrent chat turn has since advanced.

        Never moves the anchor backwards — a slow action that started before
        a chat turn must not un-do that turn's newer anchor. Returns whether a
        row was actually updated (``False`` for an unknown character *and* for
        an already-newer anchor)."""

    async def delete(self, character_id: str) -> bool:
        """Remove the character. Returns True when a row was removed."""


class ConversationRepositoryPort(Protocol):
    async def get(self, conversation_id: str) -> Conversation | None:
        """Fetch conversation by id."""

    async def save(
        self, conversation: Conversation, *, truncation: bool = False,
    ) -> None:
        """Store the whole conversation (replace path).

        ``truncation=True`` marks a deliberate, authoritative shrink (an undo):
        the replace applies verbatim and skips the stale-merge entirely, so a
        concurrently-appended tail the user is truncating past is not merged back
        (B3 — truncation is authoritative). The default (``False``) keeps the
        optimistic-concurrency merge that preserves concurrent CAS appends.
        """

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

    async def latest_page_for_character(
        self,
        character_id: str,
        *,
        source: str | None = "web",
        limit: int,
        before_position: int | None = None,
    ) -> ConversationMessagePage | None:
        """The newest ``limit`` messages of the same conversation
        :meth:`latest_for_character` would return (IV10).

        **Which conversation counts as "latest" is identical** — implementations
        must share one selection query with :meth:`latest_for_character` rather
        than re-deriving it. Only the message layer is truncated: the chat panel
        used to render an entire thread (316 messages in the reported case) into
        the DOM on open, and every picture in it stayed decoded in memory
        whether or not it was on screen.

        Pagination follows the feed's keyset shape (plan D8): ``limit`` +
        ``before_position`` in, ``has_more`` / ``next_before`` out. The cursor is
        ``MessageRow.position`` — the in-conversation ordering column the thread
        is already sorted by, unique per conversation, and stable under
        concurrent appends because appends only ever extend the tail.
        ``before_position`` is exclusive: only messages strictly older are
        returned.

        Returns ``None`` only when there is no such conversation at all — a
        conversation with no messages answers with an empty page. ``limit <= 0``
        yields an empty page, mirroring
        :meth:`recent_messages_for_character`.
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

    async def append_messages(
        self,
        conversation_id: str,
        *,
        expected_next_position: int,
        messages: list[Message],
        idempotency_key: str | None = None,
    ) -> AppendResult:
        """CAS-append ``messages`` to the tail of a conversation (LH2).

        The external-chat turn machine (DR-LH0-004) forbids the
        whole-conversation ``save()`` replace as an external concurrency
        primitive — a lost update there would silently drop a concurrent
        turn's rows. This appends **without clearing** existing rows: it
        locks the conversation (``SELECT ... FOR UPDATE``; under SQLite the
        environment is single-writer so a plain re-read suffices), verifies
        ``max(position) + 1 == expected_next_position`` (``0`` for a
        message-less conversation), and on match inserts each supplied message
        at consecutive positions. A position mismatch returns
        ``AppendResult(ok=False, conflict=True)`` and writes nothing. The
        conversation row must already exist — the turn service resolves /
        creates the ``source="line"`` conversation before driving the seam.

        ``idempotency_key`` (B1) makes a single-message append turn-idempotent:
        when supplied, an append whose key already exists on this conversation
        returns the EXISTING row's ``AppendResult(ok=True)`` instead of
        inserting a duplicate. This closes the crash window between a durable
        user-turn append and the receipt ``record_user_turn`` — a takeover
        re-appending under the same key adopts the original row rather than
        writing a second copy. Only valid for a single-message append.
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
