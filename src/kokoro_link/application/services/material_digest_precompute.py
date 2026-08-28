"""DIGEST_OFFPATH — the material digest is budgeted *after* a turn.

The prompt material digest (:class:`PromptMaterialDigest`) used to be one
of wave 2's three aux-LLM calls in :mod:`chat_turn_aux`: every chat turn
paid a full upstream round trip in front of the first token so the prompt
could carry a dozen condensed bullets instead of the raw source blocks.

Its inputs — emotion events, self reflections, story events, the arc and
its upcoming beats, the character's recent feed posts — are *slow*. None
of them changes between two turns except through the post-turn that runs
between them. So the digest is now produced once per turn on the
**post-turn** side, where nothing is waiting for it, and the chat path
only reads what is already there:

* **read** (:meth:`MaterialDigestPrecomputer.cached`) — one indexed
  primary-key SELECT, no upstream call. It rides in wave 1 alongside the
  other DB loads, so it costs the turn no wall clock that the slowest of
  those reads was not already spending. A miss returns ``None``, which is
  the digest's long-standing "render the source blocks instead" path,
  fully supported by the prompt builder. The chat path **never** computes
  inline: doing so would put the very latency this removes back on the
  turn, at the worst possible moment (a cold store is exactly a player's
  first turn).
* **write** (:meth:`MaterialDigestPrecomputer.recompute`) — awaited at the
  end of the post-turn body, after that body's own writes have landed, so
  the digest it produces already contains the turn that just happened.

The cost is that the digest a turn reads was budgeted by the *previous*
turn — stale by one turn, which the owner accepted for material this
slow-moving. Two properties keep "one turn" from quietly becoming "any
number of turns":

* a ``recompute`` that produces nothing **deletes** the row it could not
  refresh, so a broken digester degrades to source blocks rather than
  serving older and older bullets;
* a read refuses any row older than :data:`_MAX_AGE`. Rows do not expire
  on their own, and a player who comes back after a month would otherwise
  be handed a month-old summary of "recent" material — worse than the
  source blocks, because it reads as current.

Storage is a table, not a process-local dict, and that is the load-bearing
decision here. Where a post-turn enqueuer is wired the post-turn body runs
on a **worker** process while chat is served from the **API** process:
anything held in memory is written by one and read by the other, which is
to say never read at all. A row crosses that boundary, and it makes the
undo's invalidation cross it too.

Two writers can reach that row at once — nothing serialises post-turn jobs
per character — so ``updated_at`` doubles as a version. It is stamped at
the instant the *source material* was read, before the upstream call, so
"newer" means "saw more recent material" rather than "finished later"; the
store refuses any write or self-withdrawal carrying an older stamp. Only
the turn undo deletes unconditionally, because a reversed turn's material
has to go whoever wrote last.

The key is ``(character_id, operator_id)`` and the row carries the
``content_tolerance`` it was digested under. A tolerance mismatch is a
**miss**, not a stale hit: the digest text is generated to a tolerance,
and handing an NSFW-mode digest to a normal-mode prompt (or the reverse)
would move content across the boundary the tolerance exists to hold.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Protocol

from kokoro_link.contracts.prompt_material_digest import (
    PromptMaterialDigest,
    PromptMaterialDigestStorePort,
    StoredPromptMaterialDigest,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.emotion_event import EmotionEvent
from kokoro_link.domain.entities.feed_post import FeedPost
from kokoro_link.domain.entities.operator_profile import (
    DEFAULT_OPERATOR_ID,
    OperatorProfile,
)
from kokoro_link.domain.entities.story_arc import StoryArc, StoryArcBeat
from kokoro_link.domain.entities.story_event import StoryEvent

_LOGGER = logging.getLogger(__name__)

_MAX_AGE = timedelta(hours=24)
"""How old a budgeted digest may be and still be rendered.

A row has no natural expiry: the post-turn overwrites it, and a player
who stops chatting simply leaves the last one sitting there. Without a
ceiling, someone returning after a month opens the app and the prompt
tells the character that these are the *recent* emotional beats — a
confidently current summary of a month-old world, which is strictly worse
than the source blocks the miss falls back to (those are re-read every
turn and are never wrong about their own age).

Twenty-four hours because that is the window the digest's own largest
input already uses: ``_load_recent_emotion_events`` reads the last 24h.
A digest older than its inputs' horizon is summarising material the next
read would not even look at.
"""

__all__ = [
    "MaterialDigestInputs",
    "MaterialDigestInvalidator",
    "MaterialDigestLoaders",
    "MaterialDigestPrecomputer",
    "UndoneTurnCheck",
    "digest_operator_id",
]


class UndoneTurnCheck(Protocol):
    """``UndoneTurnGate``'s reading half, as ``recompute`` needs it.

    Taken as a collaborator rather than reached for on the service so the
    race below can be tested without standing up a gate and its
    repository — and so this module keeps depending on nothing but ports.
    """

    async def is_undone(self, turn_record_id: str | None) -> bool: ...


class MaterialDigestInvalidator(Protocol):
    """The one method the turn undo needs from the precomputer.

    Named separately so ``turn_undo`` depends on "something that can
    forget a character's digest" rather than on the whole precomputer —
    and so the undo step can be tested with three lines of stub.
    """

    async def invalidate(
        self, character_id: str, operator_id: str | None = None,
    ) -> int: ...


def digest_operator_id(
    character: Character, operator: OperatorProfile | None,
) -> str:
    """The operator half of the key.

    Derived exactly as ``ChatService._load_prompt_material_digest`` derives
    the id it stamps into the digest context — one definition, so the turn
    that writes a row and the turn that reads it cannot disagree about
    whose digest it is.
    """
    return getattr(operator, "id", None) or getattr(
        character, "user_id", DEFAULT_OPERATOR_ID,
    )


def _as_utc(value: datetime) -> datetime:
    return (
        value.astimezone(timezone.utc)
        if value.tzinfo
        else value.replace(tzinfo=timezone.utc)
    )


@dataclass(frozen=True, slots=True)
class MaterialDigestInputs:
    """The freshly-read source material one digest is built from."""

    emotion_events: list[EmotionEvent]
    self_reflections: list
    story_events: list[StoryEvent]
    story_arc: StoryArc | None
    upcoming_arc_beats: list[StoryArcBeat]
    recent_feed_posts: tuple[FeedPost, ...]


class MaterialDigestLoaders(Protocol):
    """The slice of ``ChatService`` this module drives.

    Same arrangement as ``chat_turn_aux.TurnAuxLoaders``: the loaders stay
    bound methods on the service (they read its wired repositories), and
    this protocol only names the collaboration so the precomputer can be
    exercised against a stub.

    Every method here is read-only on purpose. The chat path reaches the
    same material through ``_ensure_story_events`` / ``_ensure_story_arc``,
    which roll the daily gacha and may auto-start an arc; a background
    budgeting pass must not be the thing that creates a character's story.
    """

    async def _load_recent_emotion_events(
        self,
        *,
        character_id: str,
        operator: OperatorProfile | None,
        now: datetime,
    ) -> list[EmotionEvent]: ...

    async def _load_self_reflections(
        self,
        *,
        character_id: str,
        operator: OperatorProfile | None,
    ) -> list: ...

    async def _load_recent_feed_posts(
        self, character_id: str,
    ) -> tuple[FeedPost, ...]: ...

    async def _load_material_digest_story_inputs(
        self,
        *,
        character: Character,
        today: date | None,
    ) -> tuple[list[StoryEvent], StoryArc | None, list[StoryArcBeat]]: ...

    async def _load_prompt_material_digest(
        self,
        *,
        character: Character,
        operator: OperatorProfile | None,
        emotion_events: list[EmotionEvent],
        self_reflections: list,
        story_events: list[StoryEvent] | None,
        story_arc: StoryArc | None,
        upcoming_arc_beats: list[StoryArcBeat] | None,
        recent_feed_posts: tuple[FeedPost, ...],
        content_tolerance: str,
    ) -> PromptMaterialDigest | None: ...


class MaterialDigestPrecomputer:
    """Reads and writes one store's worth of budgeted digests.

    Holds no state of its own — the store is the state. One instance per
    ``ChatService``, and the same instance is handed to the turn undo so
    "forget what that turn budgeted" reaches the very rows chat reads.
    """

    def __init__(
        self,
        store: PromptMaterialDigestStorePort,
        *,
        max_age: timedelta = _MAX_AGE,
    ) -> None:
        self._store = store
        self._max_age = max_age

    # -- read (chat path) --------------------------------------------
    async def cached(
        self,
        *,
        character_id: str,
        operator_id: str,
        content_tolerance: str,
        now: datetime,
    ) -> PromptMaterialDigest | None:
        """The digest budgeted by the previous turn, or ``None``.

        Fail-open on every branch — a wrong tolerance, an over-age row, a
        store that raises. All three mean the same thing to the caller
        ("render the source blocks"), and none of them is worth failing a
        player's turn over.
        """
        try:
            stored = await self._store.get(
                character_id=character_id, operator_id=operator_id,
            )
        except Exception:
            _LOGGER.exception(
                "material digest read failed character=%s operator=%s; "
                "rendering source blocks",
                character_id, operator_id,
            )
            return None
        if stored is None:
            return None
        if stored.content_tolerance != content_tolerance:
            # Not stale — *wrong*. See the module docstring.
            return None
        if _as_utc(now) - _as_utc(stored.updated_at) > self._max_age:
            return None
        return stored.digest

    # -- write (post-turn path) --------------------------------------
    async def recompute(
        self,
        loaders: MaterialDigestLoaders,
        *,
        character: Character,
        operator: OperatorProfile | None,
        content_tolerance: str,
        now: datetime,
        today: date | None = None,
        turn_record_id: str | None = None,
        undone_turn_gate: UndoneTurnCheck | None = None,
    ) -> PromptMaterialDigest | None:
        """Re-read the source material and budget the next turn's digest.

        Never raises. It is awaited from the post-turn body, whose whole
        discipline is that an enrichment failure costs the enrichment and
        nothing else — and the enrichment's own absence (``None``) is a
        render the prompt builder has always supported.

        *now* is resolved by the caller **before** the source reads, so it
        is the instant this digest's material was true. It is written as
        ``updated_at`` and the store compares on it: of two concurrent
        post-turns, the one that read later wins regardless of which
        finishes first.

        ``turn_record_id`` + ``undone_turn_gate`` close the undo race —
        see :meth:`_withdraw_if_undone`. Both optional so a caller with no
        turn to name (and every unit test of the budget itself) simply
        does not run that check.
        """
        character_id = character.id
        operator_id = digest_operator_id(character, operator)
        try:
            inputs = await self._load_inputs(
                loaders,
                character=character,
                operator=operator,
                now=now,
                today=today,
            )
            digest = await loaders._load_prompt_material_digest(
                character=character,
                operator=operator,
                emotion_events=inputs.emotion_events,
                self_reflections=inputs.self_reflections,
                story_events=inputs.story_events,
                story_arc=inputs.story_arc,
                upcoming_arc_beats=inputs.upcoming_arc_beats,
                recent_feed_posts=inputs.recent_feed_posts,
                content_tolerance=content_tolerance,
            )
        except Exception:
            _LOGGER.exception(
                "material digest precompute failed character=%s operator=%s",
                character_id, operator_id,
            )
            return None

        stamp = _as_utc(now)
        try:
            if digest is None:
                # Nothing to serve. Drop whatever the previous turn left
                # so the next turn renders source blocks rather than a
                # digest of unbounded age — "stale by one turn" is the
                # whole bargain.
                #
                # Bounded by our own stamp: arriving late with nothing to
                # say is not a licence to delete a *newer* digest somebody
                # else successfully produced in the meantime.
                await self._store.delete(
                    character_id=character_id,
                    operator_id=operator_id,
                    not_newer_than=stamp,
                )
                return None
            landed = await self._store.upsert(
                StoredPromptMaterialDigest(
                    character_id=character_id,
                    operator_id=operator_id,
                    content_tolerance=content_tolerance,
                    digest=digest,
                    updated_at=stamp,
                ),
            )
        except Exception:
            _LOGGER.exception(
                "material digest store write failed character=%s operator=%s",
                character_id, operator_id,
            )
            return None
        if not landed:
            # A fresher read is already stored. Nothing of ours is in the
            # store, so there is nothing to withdraw either.
            _LOGGER.info(
                "material digest superseded by a newer read character=%s "
                "operator=%s",
                character_id, operator_id,
            )
            return None
        await self._withdraw_if_undone(
            character_id=character_id,
            operator_id=operator_id,
            stamp=stamp,
            turn_record_id=turn_record_id,
            undone_turn_gate=undone_turn_gate,
        )
        return digest

    async def _withdraw_if_undone(
        self,
        *,
        character_id: str,
        operator_id: str,
        stamp: datetime,
        turn_record_id: str | None,
        undone_turn_gate: UndoneTurnCheck | None,
    ) -> None:
        """Write-then-validate: take the row back if the turn was reversed.

        The post-turn's own undo gate is checked before this body's writes
        begin, but *this* write is the last one and it sits behind two or
        three upstream round trips (the persona pass, the checkpoint
        merge, the digester itself). Seconds. An undo landing anywhere in
        that window runs its whole rollback — including the step that
        deletes this row — and then watches a late ``upsert`` put the
        reversed turn's emotion events and arc snapshot straight back, for
        the next prompt to read.

        Re-asking after the write closes it, and the two interleavings are
        both covered because the tombstone is raised *before* the undo's
        delete step:

        * undo's delete lands first, our upsert second → the tombstone was
          already up when we ask, so we see it and withdraw;
        * we ask first, the tombstone goes up after → the undo's own
          delete step has not run yet, and when it does it removes our
          row.

        No stale latch (the DH3 answer) is needed here: that exists for a
        summary nothing can un-merge, whereas this row is rebuilt by the
        next post-turn or aged out within a day.

        The delete is bounded by our own stamp so a withdrawal cannot take
        out a newer digest, and a gate that raises is treated as "undone".
        Withdrawing costs one turn of source blocks; not withdrawing shows
        the player material they just took back.
        """
        if not turn_record_id or undone_turn_gate is None:
            return
        try:
            undone = await undone_turn_gate.is_undone(turn_record_id)
        except Exception:
            _LOGGER.exception(
                "material digest undo re-check failed turn=%s; withdrawing "
                "the row rather than risk serving a reversed turn",
                turn_record_id,
            )
            undone = True
        if not undone:
            return
        try:
            await self._store.delete(
                character_id=character_id,
                operator_id=operator_id,
                not_newer_than=stamp,
            )
        except Exception:
            _LOGGER.exception(
                "material digest withdrawal failed character=%s operator=%s "
                "turn=%s",
                character_id, operator_id, turn_record_id,
            )
            return
        _LOGGER.info(
            "material digest withdrawn: turn %s was undone while the digest "
            "was in flight (character=%s)",
            turn_record_id, character_id,
        )

    # -- invalidate (undo path) --------------------------------------
    async def invalidate(
        self, character_id: str, operator_id: str | None = None,
    ) -> int:
        """Forget what a reversed turn budgeted (TU series).

        ``operator_id=None`` drops every operator's row for the character.
        The undo journal names a character but not an operator, and
        over-forgetting costs one turn of source-block rendering while
        under-forgetting would feed a reversed turn's material into the
        next prompt.

        Fail-soft like every undo step: a store that raises is logged and
        reported as "forgot nothing", never allowed to abort the rollback.
        """
        try:
            return await self._store.delete(
                character_id=character_id, operator_id=operator_id,
            )
        except Exception:
            _LOGGER.exception(
                "material digest invalidate failed character=%s", character_id,
            )
            return 0

    # -- internals ----------------------------------------------------
    async def _load_inputs(
        self,
        loaders: MaterialDigestLoaders,
        *,
        character: Character,
        operator: OperatorProfile | None,
        now: datetime,
        today: date | None,
    ) -> MaterialDigestInputs:
        """Read every source at once — four reads, six inputs.

        ``gather`` without ``return_exceptions``: each of these loaders
        already swallows its own failure and returns an empty fallback,
        and anything that still escapes belongs to the caller's single
        ``except``, which turns it into "no digest this turn".
        """
        emotion_events, self_reflections, recent_feed_posts, story = (
            await asyncio.gather(
                loaders._load_recent_emotion_events(
                    character_id=character.id, operator=operator, now=now,
                ),
                loaders._load_self_reflections(
                    character_id=character.id, operator=operator,
                ),
                loaders._load_recent_feed_posts(character.id),
                loaders._load_material_digest_story_inputs(
                    character=character, today=today,
                ),
            )
        )
        story_events, story_arc, upcoming_arc_beats = story
        return MaterialDigestInputs(
            emotion_events=list(emotion_events or []),
            self_reflections=list(self_reflections or []),
            story_events=list(story_events or []),
            story_arc=story_arc,
            upcoming_arc_beats=list(upcoming_arc_beats or []),
            recent_feed_posts=tuple(recent_feed_posts or ()),
        )
