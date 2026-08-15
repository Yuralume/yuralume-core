"""Wrapping a 起幕 scene up and landing it as canon (SC1-D).

Three routes reach this module and all three run the same steps in the
same order — the verdict after an in-scene turn, the player's 「結束場景」
button, and (SC1-E) the idle-timeout sweep. Sharing the core is not
tidiness: the red line that a wrap-up must never invent player actions
 is only as strong as its weakest path, and the
timeout path — the one nobody is watching — is exactly where a second
implementation would drift.

The order of the steps is the design:

1. **Write first, close second.** The wrap-up is generated before the
   session row is touched, so a replica that loses the close race throws
   away prose instead of leaving a closed scene it never got to narrate.
2. **The close is a compare-and-set.** Losing it is an ordinary outcome
   (someone else wrapped this scene up) and returns idempotently — no
   second narration in the thread, no second beat realization.
3. **Everything after the close is fail-soft.** Once the row says
   ``closed`` the player is unstuck, which is the wrap-up's actual
   obligation; a thread append or a canon write that fails afterwards is
   logged and lived with, never rolled back into a re-opened scene.

**When the writer produces nothing** the scene still closes — that is the
point of step 3 — and canon falls back to the one piece of prose about
this scene that is already known to be real and known to contain no
player actions: its own opening narration, still sitting in the thread.
That is a deliberately lossy record, not a fabricated one. Landing
nothing at all was the alternative and it is worse: the beat would stay
pending, and the next 起幕 would re-enact a scene the player can still
read further up the same thread.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date as date_type, datetime, timezone
from typing import Sequence

from kokoro_link.application.services.memory_embedding import attach_embeddings
from kokoro_link.application.services.story_scene_thread import (
    append_scene_messages,
)
from kokoro_link.contracts.clock import ensure_utc
from kokoro_link.contracts.embedder import EmbedderPort
from kokoro_link.contracts.memory import MemoryRepositoryPort
from kokoro_link.contracts.repositories import ConversationRepositoryPort
from kokoro_link.contracts.story_scene import (
    SCENE_NARRATION_SPEAKER,
    SCENE_PLAYER_SPEAKER,
    StorySceneClosingContext,
    StorySceneClosingDraft,
    StorySceneCloserPort,
    StorySceneSessionRepositoryPort,
    render_scene_line,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.conversation import (
    Conversation,
    Message,
    MessageKind,
    MessageRole,
)
from kokoro_link.domain.entities.memory_item import MemoryItem
from kokoro_link.domain.entities.story_scene_session import (
    SCENE_CLOSE_RESOLVED,
    StorySceneSession,
)
from kokoro_link.domain.value_objects.content_flow import (
    CONTENT_TOLERANCE_FRONTIER,
    sanitize_messages_for_tolerance,
)
from kokoro_link.domain.value_objects.memory_kind import MemoryKind


_LOGGER = logging.getLogger(__name__)

_TRANSCRIPT_MESSAGE_LIMIT = 40
"""How much of the scene the wrap-up reads.

The whole scene, in practice — a 起幕 scene is a handful of exchanges,
not a channel history — with a ceiling so a scene somebody played for an
hour cannot turn one background call into a very large one."""

_TRANSCRIPT_LINE_CHARS = 400

_SIDE_STORY_SALIENCE = 0.55
"""Where an unplanned but played-through scene sits.

Above the daily gacha entry (0.45): the player pulled this one and acted
inside it. Below a realized arc beat's climax shapes, which the arc path
assigns from the beat's own tension — a side story has no such spine."""

_SIDE_STORY_TAGS = ("story_event", "story_scene", "side_story")
"""``story_event`` first so this memory is treated exactly like the ones
the arc path writes; the other two make a scene-born memory findable
without having to infer it from prose."""

_DEGRADED_TAG = "story_scene_unnarrated"
"""Marks canon that fell back to the opening narration because the
wrap-up writer produced nothing. Visible in the row rather than only in a
log line, so an operator reading a thin memory can tell "the writer
failed here" from "the scene really was that small"."""


@dataclass(frozen=True, slots=True)
class SceneClosing:
    """The outcome of wrapping one scene up."""

    session: StorySceneSession
    """The closed session — the row as it now stands, whoever closed it."""
    closing_narration: Message | None = None
    """The wrap-up appended to the thread, or ``None``.

    ``None`` covers three ordinary cases the caller does not need to tell
    apart: the writer produced no prose, the thread could not be written
    to, and this call lost the close race to someone who already wrote
    it. In all three the scene is closed and the client re-reads the
    thread to see whatever is actually there."""
    canon_landed: bool = False
    already_closed: bool = False
    """This call found the scene already closed and changed nothing."""


class SceneClosingCoordinator:
    """Runs the shared wrap-up for all three close routes."""

    def __init__(
        self,
        *,
        sessions: StorySceneSessionRepositoryPort,
        conversations: ConversationRepositoryPort,
        closer: StorySceneCloserPort | None,
        story_event_service=None,  # noqa: ANN001 - optional, duck-typed
        memory_repository: MemoryRepositoryPort | None = None,
        embedder: EmbedderPort | None = None,
    ) -> None:
        self._sessions = sessions
        self._conversations = conversations
        self._closer = closer
        self._story_events = story_event_service
        self._memories = memory_repository
        self._embedder = embedder

    # ── verdict ──────────────────────────────────────────────────────

    async def judge(
        self,
        character: Character,
        *,
        session: StorySceneSession,
        conversation: Conversation | None = None,
        today: date_type | None = None,
        language: str = "zh-TW",
    ) -> StorySceneClosingDraft | None:
        """Is this scene finished? ``None`` whenever it is not, or unknown.

        Failure and "not yet" deliberately share an answer: a writer that
        crashed must not end a scene the player is still inside, so there
        is only one direction for this call to fail in.
        """
        draft = await self._write(
            character,
            session=session,
            mode=SCENE_CLOSE_RESOLVED,
            conversation=conversation,
            today=today,
            language=language,
        )
        if draft is None or not draft.resolved:
            return None
        return draft

    # ── close ────────────────────────────────────────────────────────

    async def close(
        self,
        character: Character,
        *,
        session: StorySceneSession,
        mode: str,
        now: datetime,
        draft: StorySceneClosingDraft | None = None,
        conversation: Conversation | None = None,
        today: date_type | None = None,
        language: str = "zh-TW",
    ) -> SceneClosing:
        moment = ensure_utc(now)
        scene_thread = await self._scene_thread(session, conversation)
        if draft is None:
            draft = await self._write(
                character,
                session=session,
                mode=mode,
                conversation=scene_thread,
                today=today,
                language=language,
            )

        closed = await self._sessions.close(
            session.id, reason=mode, at=moment,
        )
        if closed is None:
            # Someone else wrapped this scene up between the caller's read
            # and this write. Report what the row says and write nothing:
            # a second closing narration in the thread would read as the
            # scene ending twice.
            existing = await self._sessions.get(session.id)
            return SceneClosing(
                session=existing or session.closed(reason=mode, at=moment),
                already_closed=True,
            )

        narration = await self._append_narration(
            closed, draft=draft, now=moment,
        )
        canon_landed = await self._land_canon(
            character,
            session=closed,
            draft=draft,
            scene_thread=scene_thread,
            now=moment,
        )
        return SceneClosing(
            session=closed,
            closing_narration=narration,
            canon_landed=canon_landed,
        )

    # ── steps ────────────────────────────────────────────────────────

    async def _write(
        self,
        character: Character,
        *,
        session: StorySceneSession,
        mode: str,
        conversation: Conversation | None,
        today: date_type | None,
        language: str,
    ) -> StorySceneClosingDraft | None:
        if self._closer is None:
            return None
        scene_thread = await self._scene_thread(session, conversation)
        transcript, player_lines = render_scene_transcript(
            scene_thread, session=session, character=character,
        )
        try:
            return await self._closer.write_closing(
                StorySceneClosingContext(
                    character=character,
                    session=session,
                    mode=mode,
                    transcript=transcript,
                    player_lines=player_lines,
                    today=today,
                    operator_primary_language=language,
                ),
            )
        except Exception:  # noqa: BLE001 - the port owns its own failures
            _LOGGER.exception(
                "story scene closer crashed scene=%s mode=%s",
                session.id,
                mode,
            )
            return None

    async def _scene_thread(
        self, session: StorySceneSession, conversation: Conversation | None,
    ) -> Conversation | None:
        """The thread the scene was played in.

        The caller's snapshot wins when it has one: on the in-turn path it
        already contains the reply that just landed, and re-reading would
        both cost a round trip and risk missing it.
        """
        if conversation is not None:
            return conversation
        try:
            return await self._conversations.get(session.conversation_id)
        except Exception:  # noqa: BLE001
            _LOGGER.exception(
                "story scene wrap-up could not read the thread scene=%s",
                session.id,
            )
            return None

    async def _append_narration(
        self,
        session: StorySceneSession,
        *,
        draft: StorySceneClosingDraft | None,
        now: datetime,
    ) -> Message | None:
        text = (draft.closing_narration if draft else "") or ""
        if not text.strip():
            return None
        message = Message(
            role=MessageRole.ASSISTANT,
            content=text,
            kind=MessageKind.SCENE_NARRATION,
            created_at=now,
        )
        # Always onto the thread the session pinned at open time, never
        # onto whichever conversation the caller happened to be holding:
        # a wrap-up written into a different thread would strand the
        # scene it belongs to.
        try:
            landed = await append_scene_messages(
                self._conversations, session.conversation_id, [message],
            )
        except Exception:  # noqa: BLE001
            _LOGGER.exception(
                "story scene closing narration append crashed scene=%s",
                session.id,
            )
            return None
        if not landed:
            _LOGGER.warning(
                "story scene closed without a visible wrap-up scene=%s "
                "conversation=%s",
                session.id,
                session.conversation_id,
            )
            return None
        return message

    async def _land_canon(
        self,
        character: Character,
        *,
        session: StorySceneSession,
        draft: StorySceneClosingDraft | None,
        scene_thread: Conversation | None,
        now: datetime,
    ) -> bool:
        summary = (draft.canon_summary if draft else "") or ""
        degraded = False
        if not summary.strip():
            summary = _opening_narration(scene_thread, session=session)
            degraded = bool(summary)
        if not summary.strip():
            _LOGGER.warning(
                "story scene closed with nothing to land as canon scene=%s "
                "beat=%s layer=%s",
                session.id,
                session.beat_id,
                session.source_layer,
            )
            return False
        if session.beat_id is not None:
            return await self._realize_beat(
                character,
                session=session,
                narrative=summary,
                tone=draft.emotional_tone if draft else None,
                now=now,
            )
        return await self._remember_side_story(
            session=session,
            narrative=summary,
            now=now,
            degraded=degraded,
        )

    async def _realize_beat(
        self,
        character: Character,
        *,
        session: StorySceneSession,
        narrative: str,
        tone: str | None,
        now: datetime,
    ) -> bool:
        """Layers 1 and 2 land through the existing realization chain.

        Reused rather than reimplemented because that chain does three
        things a scene close must not get subtly different: the StoryEvent
        row, the episodic memory shaped by the beat's tension, and the
        relationship milestone when the beat completes its arc.
        """
        if self._story_events is None:
            return False
        try:
            event = await self._story_events.record_arc_beat_realization(
                character,
                beat_id=session.beat_id,
                narrative=narrative,
                now=now,
                emotional_tone=tone,
            )
        except Exception:  # noqa: BLE001 - the session is already closed
            _LOGGER.exception(
                "story scene canon write failed scene=%s beat=%s",
                session.id,
                session.beat_id,
            )
            return False
        return event is not None

    async def _remember_side_story(
        self,
        *,
        session: StorySceneSession,
        narrative: str,
        now: datetime,
        degraded: bool,
    ) -> bool:
        """Layer 3 lands as a plain episodic memory — no beat, no arc.

        Written with the same shape the story-event path uses, including
        leaving ``audience`` unjudged: §3.4 #4 says scene memories follow
        the existing audience rules, and stamping one here would be a new
        rule rather than the existing one.
        """
        if self._memories is None:
            return False
        tags = list(_SIDE_STORY_TAGS)
        if degraded:
            tags.append(_DEGRADED_TAG)
        try:
            item = MemoryItem.create(
                character_id=session.character_id,
                kind=MemoryKind.EPISODIC,
                content=narrative,
                salience=_SIDE_STORY_SALIENCE,
                tags=tags,
                created_at=now,
            )
            embedded = await attach_embeddings([item], self._embedder)
            await self._memories.add_many(embedded)
        except Exception:  # noqa: BLE001 - the session is already closed
            _LOGGER.exception(
                "story scene side-story memory write failed scene=%s",
                session.id,
            )
            return False
        return True


# ── transcript ───────────────────────────────────────────────────────


def render_scene_transcript(
    conversation: Conversation | None,
    *,
    session: StorySceneSession,
    character: Character,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The scene as it was played: labelled lines, and the player's own.

    Two returns rather than one because they are read for different
    reasons. The labelled transcript is what the model writes from; the
    bare player lines are the evidence its citations are checked against
    (§3.4 #1), and re-deriving them from labelled prose downstream would
    mean parsing our own formatting back out.

    Scoped by ``opened_at`` so an earlier scene in the same thread — and
    in particular *its* closing narration — cannot be read as part of
    this one.
    """
    if conversation is None:
        return (), ()
    opened_at = ensure_utc(session.opened_at)
    in_scene = [
        message for message in conversation.messages
        if message.kind is not MessageKind.TOOL_ONLY
        and _created_at(message) >= opened_at
    ]
    # The same tolerance the side-story material provider applies when it
    # feeds recent dialogue to a story writer: the wrap-up is a background
    # call whose model is not necessarily the one that played the scene.
    in_scene = sanitize_messages_for_tolerance(
        in_scene[-_TRANSCRIPT_MESSAGE_LIMIT:],
        content_tolerance=CONTENT_TOLERANCE_FRONTIER,
    )
    lines: list[str] = []
    player_lines: list[str] = []
    for message in in_scene:
        text = _clip((message.content or "").strip())
        if not text:
            continue
        if message.kind is MessageKind.SCENE_NARRATION:
            speaker = SCENE_NARRATION_SPEAKER
        elif message.role is MessageRole.USER:
            speaker = SCENE_PLAYER_SPEAKER
            player_lines.append(text)
        else:
            speaker = character.name
        lines.append(render_scene_line(speaker, text))
    return tuple(lines), tuple(player_lines)


def _opening_narration(
    conversation: Conversation | None, *, session: StorySceneSession,
) -> str:
    """This scene's own opening narration, if it is still readable.

    The degraded canon record. It is the only prose about this scene that
    is known to be real (a model wrote it, the player read it) and known
    to contain no player actions (the opener is forbidden to write any),
    which is what makes it usable as a factual record and a template-
    assembled sentence not."""
    if conversation is None:
        return ""
    opened_at = ensure_utc(session.opened_at)
    for message in conversation.messages:
        if (
            message.kind is MessageKind.SCENE_NARRATION
            and _created_at(message) >= opened_at
        ):
            return (message.content or "").strip()
    return ""


def _created_at(message: Message) -> datetime:
    value = getattr(message, "created_at", None)
    if value is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    return ensure_utc(value)


def _clip(text: str) -> str:
    if len(text) > _TRANSCRIPT_LINE_CHARS:
        return text[:_TRANSCRIPT_LINE_CHARS].rstrip() + "…"
    return text


__all__: Sequence[str] = (
    "SceneClosing",
    "SceneClosingCoordinator",
    "render_scene_transcript",
)
