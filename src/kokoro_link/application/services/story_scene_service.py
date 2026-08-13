"""起幕 — the player pulls the next piece of story instead of waiting.

SC1-A implements the first layer of the §3.1 waterfall end to end: the
active arc's next pending beat becomes an opening (narration + the
character's first line + a visible scene frame), the opening lands in the
character's ordinary web thread, and a ``story_scene_sessions`` row marks
the character as being inside a scene until SC1-D/E close it.

Four behaviours are worth reading the code for, because they are choices
rather than mechanics:

* **The waterfall is a list, not a chain of ifs.** ``material_providers``
  is walked in order and the first answer wins, so SC1-B's forced-season
  and side-story layers are two appended objects, not an edit here.
* **Failure writes nothing, and (hosted) charges nothing.** The opening is
  generated *before* any row is created, and the session row is deleted
  again if the messages cannot be appended. A player who was charged for
  an opening that never happened is the hosted red line (§3.4 #2), and a
  half-open scene would lock them out of the button until the timeout
  closer ran. The one price is therefore raised as late as it can be —
  after every refusal this service can answer on its own — and released
  by the same exception that fails the action (SC3-C).
* **A player pull is not a retry.** Staging attempts are recorded with a
  non-failure result, so pressing 起幕 never spends the autonomous scene
  writer's retry budget for that beat (SC0 / §10 #3).
* **The scene is claimed in the database.** "One live scene per
  character" is a partial unique index, not a read-then-write here; two
  taps landing on two replicas is a race the schema settles.

SC1-D added the other end. Three routes can wrap a scene up — the verdict
after an in-scene turn (:meth:`close_if_resolved`), the player's
「結束場景」 button (:meth:`end_scene`), and SC1-E's idle sweep — and all
three funnel into :meth:`close_scene`, whose steps live in
``story_scene_closing``. One implementation on purpose: the red line that
a wrap-up must never invent player actions is only as strong as its
weakest path, and the timeout path is the one nobody is watching.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date as date_type, datetime, timezone, tzinfo
from typing import Sequence

from kokoro_link.application.services.chat_turn_lease import (
    ChatTurnLease,
    release_turn_lease,
)
from kokoro_link.application.services.cloud_action_billing_service import (
    CloudActionBillingService,
    NullActionBillingService,
)
from kokoro_link.application.services.story_arc_service import StoryArcService
from kokoro_link.application.services.story_scene_closing import (
    SceneClosing,
    SceneClosingCoordinator,
)
from kokoro_link.application.services.story_scene_thread import (
    append_scene_messages,
)
from kokoro_link.contracts.cloud_action_billing import (
    ACTION_STORY_SCENE_OPEN,
    client_quoted_price,
)
from kokoro_link.contracts.embedder import EmbedderPort
from kokoro_link.contracts.memory import MemoryRepositoryPort
from kokoro_link.contracts.repositories import ConversationRepositoryPort
from kokoro_link.contracts.story_scene import (
    SceneSessionConflict,
    StorySceneChipsContext,
    StorySceneChipsWriterPort,
    StorySceneCloserPort,
    StorySceneMaterial,
    StorySceneMaterialProviderPort,
    StorySceneOpeningContext,
    StorySceneOpeningDraft,
    StorySceneOpenerPort,
    StorySceneSessionRepositoryPort,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.conversation import (
    SOURCE_WEB,
    Conversation,
    Message,
    MessageKind,
    MessageRole,
)
from kokoro_link.domain.entities.story_scene_session import (
    SCENE_CLOSE_MANUAL,
    SCENE_CLOSE_RESOLVED,
    StorySceneSession,
)
from kokoro_link.domain.value_objects.timezone import timezone_for_id


_LOGGER = logging.getLogger(__name__)


SCENE_OPEN_PLAY_SOURCE = "story_scene"
"""``last_play_attempt_source`` for a beat staged by the 起幕 button.

Distinct from ``scene_simulation`` (the autonomous writer) and
``chat_scene_directive`` (a beat that surfaced inside ordinary chat) so
an operator reading a beat's history can tell who pulled it."""

SCENE_OPEN_PLAY_RESULT = "scene_opened"
"""``last_play_attempt_result`` for a successful 起幕 staging.

Deliberately outside ``FAILED_PLAY_RESULTS``: the retry budget spends
*failures*, and a player choosing to play a beat is neither a failure nor
a retry. Defined here rather than in the arc entity because it is the
scene runtime's vocabulary — the arc layer only needs to know it is not a
failure, which it decides by exclusion."""

class StorySceneError(RuntimeError):
    """Base for structured 起幕 failures. ``code`` reaches the client."""

    code = "story_scene_error"


class StorySceneUnavailable(StorySceneError):
    """This account may not open story scenes at all (demo tier)."""

    code = "story_scene_unavailable"


class SceneAlreadyOpen(StorySceneError):
    """The character is already inside a scene (§2 #3)."""

    code = "scene_in_progress"

    def __init__(self, character_id: str) -> None:
        super().__init__(
            f"character {character_id} is already playing a story scene",
        )
        self.character_id = character_id


class SceneMaterialUnavailable(StorySceneError):
    """No waterfall layer could supply material for a scene.

    In SC1-A this is the common outcome for a character with no active
    arc; SC1-B's layers 2 and 3 make it rare (only a concluded series
    whose planner also failed can still land here)."""

    code = "no_material"


class SceneOpenFailed(StorySceneError):
    """The opening could not be produced or could not be persisted.

    Nothing was written and — hosted — nothing is charged."""

    code = "scene_open_failed"


class SceneNotOpen(StorySceneError):
    """There is no live scene to end (SC1-D).

    A state conflict rather than a missing resource: the character exists
    and is simply not in a scene, which is what the client's own state
    poll would have told it. Refreshing that state is the fix, so the
    route answers 409 and not 404."""

    code = "no_open_scene"


class SceneSessionMismatch(StorySceneError):
    """The scene the client asked to end is not the one that is running.

    Almost always a stale tab: the scene the player is looking at was
    closed (by the verdict, by another device, by the timeout sweep) and a
    new one was opened since. Distinguished from
    :class:`SceneNotOpen` so the client can say "that scene already
    ended" instead of "you are not in a scene", which would be a lie."""

    code = "scene_session_mismatch"

    def __init__(self, requested: str, running: str | None) -> None:
        super().__init__(
            f"story scene {requested} is not the scene currently running "
            f"({running or 'none'})",
        )
        self.requested = requested
        self.running = running


@dataclass(frozen=True, slots=True)
class StorySceneOpening:
    """What the caller needs to render a freshly opened scene."""

    session: StorySceneSession
    narration: Message
    character_message: Message
    suggested_actions: tuple[str, ...] = ()
    """Opening moves for a player who did not want to think of one.

    Empty is a perfectly good answer (no writer wired, model failed): the
    scene is already open and playable, and the composer is right there.
    """


class StorySceneService:
    def __init__(
        self,
        *,
        sessions: StorySceneSessionRepositoryPort,
        conversations: ConversationRepositoryPort,
        opener: StorySceneOpenerPort,
        material_providers: Sequence[StorySceneMaterialProviderPort],
        chips_writer: StorySceneChipsWriterPort | None = None,
        story_arc_service: StoryArcService | None = None,
        turn_lease: ChatTurnLease | None = None,
        account_runtime_profile_resolver=None,  # noqa: ANN001
        operator_profile_service=None,  # noqa: ANN001
        quota_guard=None,  # noqa: ANN001 - StorySceneQuotaGuard (SC3-B)
        local_tz: tzinfo | None = None,
        # SC1-D wrap-up collaborators. Appended rather than slotted in
        # next to their opening-path counterparts so a caller pinned to
        # the SC1-A/SC1-B signature keeps working unchanged.
        closer: StorySceneCloserPort | None = None,
        story_event_service=None,  # noqa: ANN001 - optional, duck-typed
        memory_repository: MemoryRepositoryPort | None = None,
        embedder: EmbedderPort | None = None,
        # SC3-C, appended last for the same reason as the SC1-D block: a
        # caller pinned to the earlier signature keeps working, and on
        # self-host the null object leaves every path byte-identical.
        action_billing: (
            CloudActionBillingService | NullActionBillingService | None
        ) = None,
    ) -> None:
        self._sessions = sessions
        self._conversations = conversations
        self._opener = opener
        self._material_providers = tuple(material_providers)
        self._chips_writer = chips_writer
        self._arcs = story_arc_service
        self._turn_lease = turn_lease
        self._profiles = account_runtime_profile_resolver
        self._operator_profile_service = operator_profile_service
        self._quota_guard = quota_guard
        self._local_tz = local_tz
        self._action_billing: (
            CloudActionBillingService | NullActionBillingService
        ) = action_billing or NullActionBillingService()
        # Always built, even with no writer wired: closing a scene is a
        # state transition the player depends on ("結束場景" must work on
        # a self-host deployment with no model), and only the narration
        # depends on the writer.
        self._closing = SceneClosingCoordinator(
            sessions=sessions,
            conversations=conversations,
            closer=closer,
            story_event_service=story_event_service,
            memory_repository=memory_repository,
            embedder=embedder,
        )

    # ── read ─────────────────────────────────────────────────────────

    async def current_scene(
        self, character_id: str,
    ) -> StorySceneSession | None:
        return await self._sessions.get_open_for_character(character_id)

    # ── open ─────────────────────────────────────────────────────────

    async def open_scene(
        self, character: Character, *, now: datetime | None = None,
    ) -> StorySceneOpening:
        """Raise the curtain on the next piece of this character's story.

        Raises :class:`StorySceneError` subclasses for every refusal so
        the route can map each to its own code; nothing is written unless
        the whole action succeeds.
        """
        moment = _as_utc(now)
        await self._ensure_account_may_open(character)

        live = await self._sessions.get_open_for_character(character.id)
        if live is not None:
            raise SceneAlreadyOpen(character.id)

        # SC3-B: after the live check (an already-open scene is the more
        # actionable answer), before any material work is spent.
        if self._quota_guard is not None:
            await self._quota_guard.check(character, now=moment)

        today = await self._today_for_character(character, moment)
        material = await self._resolve_material(character, today=today)
        if material is None:
            raise SceneMaterialUnavailable(
                f"no story material available for character {character.id}",
            )

        conversation = await self._resolve_conversation(character)
        # Resolved once and threaded through: the opening and the chips
        # are two calls the player reads side by side, so they must not
        # be able to disagree about which language they are written in.
        language = await self._resolve_operator_language(character)
        lease = await self._acquire_turn_lease(conversation.id)
        try:
            # SC3-C — the one price, raised last (§3.4 red line 2).
            #
            # *Every* refusal above this line is a refusal the wallet never
            # hears about: the demo gate, an already-running scene, the
            # per-tier daily ceiling, an empty waterfall and a busy thread
            # are all answers this service already knows, and charging then
            # refunding them would print a spend and a refund on the
            # player's ledger for a scene that never started.
            #
            # Inside the scope, the opening writer is the only call that can
            # be waived against this charge, so a writer that fails or
            # returns nothing leaves ``consumed`` false and the refund is
            # whole. (Forcing a new season calls the arc planner *above*
            # this line on purpose: ``arc_plan`` is a background feature
            # key and is already uncharged for a trusted deployment — plan
            # §4.2 ① — so pulling it inside the scope would buy nothing and
            # would make a planner call the reason a failed opening got
            # billed.) Success settles at scope exit; abandoning the scene
            # afterwards never refunds, because the opening was delivered.
            async with self._action_billing.action(
                ACTION_STORY_SCENE_OPEN,
                operator_id=getattr(character, "user_id", "") or "",
                quoted_price_cr=client_quoted_price(ACTION_STORY_SCENE_OPEN),
                character_origin=character.origin_official_card_id,
            ):
                draft = await self._write_opening(
                    character,
                    material=material,
                    today=today,
                    language=language,
                )
                return await self._persist_opening(
                    character,
                    conversation_id=conversation.id,
                    material=material,
                    draft=draft,
                    now=moment,
                    language=language,
                )
        finally:
            await release_turn_lease(lease)

    # ── close (SC1-D) ────────────────────────────────────────────────

    async def close_scene(
        self,
        character: Character,
        *,
        session: StorySceneSession,
        mode: str,
        now: datetime | None = None,
        verdict=None,  # noqa: ANN001 - StorySceneClosingDraft, optional
        conversation: Conversation | None = None,
        acquire_lease: bool = True,
    ) -> SceneClosing:
        """Wrap ``session`` up and land it as canon. The shared close.

        ``mode`` is the close reason the row will carry — ``resolved``,
        ``manual`` or ``timeout``. It is one vocabulary end to end: the
        value reaches the wrap-up writer as its framing, and the same
        value lands in ``closed_reason``.

        SC1-E's idle sweep calls exactly this, with
        ``mode=SCENE_CLOSE_TIMEOUT`` and the session it read from its own
        due-time query; nothing else about the timeout path needs to
        exist here.

        Never raises for a scene that was already closed — that is a race
        with an equally valid winner, reported as
        ``SceneClosing.already_closed``. ``acquire_lease=False`` is for
        the one caller that is already inside the conversation's turn
        lease (the in-turn verdict); everyone else takes it so a manual
        end cannot interleave its narration with a running reply.
        """
        moment = _as_utc(now)
        lease = (
            await self._acquire_turn_lease(session.conversation_id)
            if acquire_lease else None
        )
        try:
            return await self._closing.close(
                character,
                session=session,
                mode=mode,
                now=moment,
                draft=verdict,
                conversation=conversation,
                today=await self._today_for_character(character, moment),
                language=await self._resolve_operator_language(character),
            )
        finally:
            await release_turn_lease(lease)

    async def close_if_resolved(
        self,
        character: Character,
        *,
        session: StorySceneSession,
        conversation: Conversation | None = None,
        now: datetime | None = None,
    ) -> SceneClosing | None:
        """Ask whether the scene is over, and end it when it is.

        The in-scene chat turn's entry point. ``None`` — the usual answer
        — means the scene is still running and the caller's reply is
        unchanged in every respect, which is what keeps a scene turn
        byte-identical to one that simply has not finished yet.

        Runs *inside* the caller's turn lease, so it never takes one.
        """
        moment = _as_utc(now)
        today = await self._today_for_character(character, moment)
        language = await self._resolve_operator_language(character)
        verdict = await self._closing.judge(
            character,
            session=session,
            conversation=conversation,
            today=today,
            language=language,
        )
        if verdict is None:
            return None
        closing = await self._closing.close(
            character,
            session=session,
            mode=SCENE_CLOSE_RESOLVED,
            now=moment,
            draft=verdict,
            conversation=conversation,
            today=today,
            language=language,
        )
        # Losing the close race means someone else already wrapped this
        # scene up and their narration is in the thread; reporting ours
        # would show the player a wrap-up that was never written.
        return None if closing.already_closed else closing

    async def end_scene(
        self,
        character: Character,
        *,
        session_id: str | None = None,
        now: datetime | None = None,
    ) -> SceneClosing:
        """The player pressed 「結束場景」.

        ``session_id`` is verified against the live scene rather than
        trusted, so a stale tab ending "its" scene cannot close the one
        the player opened afterwards.
        """
        live = await self._sessions.get_open_for_character(character.id)
        if live is None:
            raise SceneNotOpen(
                f"character {character.id} is not in a story scene",
            )
        if session_id is not None and session_id != live.id:
            raise SceneSessionMismatch(session_id, live.id)
        return await self.close_scene(
            character, session=live, mode=SCENE_CLOSE_MANUAL, now=now,
        )

    # ── steps ────────────────────────────────────────────────────────

    async def _ensure_account_may_open(self, character: Character) -> None:
        """Demo accounts do not get 起幕.

        Checked against the account runtime profile rather than at the
        route, so every future caller (a chat slash-command, an external
        channel) inherits the same answer. Fail-closed on a resolver
        error, matching the album / TTS gates: an account whose policy
        cannot be read must not be handed a paid foreground action.
        """
        if self._profiles is None:
            return
        try:
            profile = await self._profiles.resolve_for_operator(
                getattr(character, "user_id", None) or "default",
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.exception(
                "story scene: runtime profile lookup failed character=%s",
                character.id,
            )
            raise StorySceneUnavailable(
                "story scenes are unavailable because the account runtime "
                "profile cannot be resolved",
            ) from exc
        if profile.is_demo:
            raise StorySceneUnavailable(
                "story scenes are not available on the demo tier",
            )

    async def _resolve_material(
        self, character: Character, *, today: date_type,
    ) -> StorySceneMaterial | None:
        """Walk the waterfall; first layer that answers wins.

        A provider that raises falls through instead of aborting: one
        broken layer must not hide the layers beneath it, which is the
        whole point of having a waterfall.
        """
        for provider in self._material_providers:
            try:
                material = await provider.resolve(character, today=today)
            except Exception:
                _LOGGER.exception(
                    "story scene material layer crashed layer=%s character=%s",
                    getattr(provider, "layer", "?"),
                    character.id,
                )
                continue
            if material is not None:
                return material
        return None

    async def _write_opening(
        self,
        character: Character,
        *,
        material: StorySceneMaterial,
        today: date_type,
        language: str,
    ) -> StorySceneOpeningDraft:
        context = StorySceneOpeningContext(
            character=character,
            material=material,
            today=today,
            operator_primary_language=language,
        )
        try:
            draft = await self._opener.write_opening(context)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.exception(
                "story scene opener crashed character=%s layer=%s",
                character.id,
                material.layer,
            )
            raise SceneOpenFailed(
                "the story scene opening could not be written",
            ) from exc
        if draft is None:
            raise SceneOpenFailed(
                "the story scene opening could not be written",
            )
        return draft

    async def _persist_opening(
        self,
        character: Character,
        *,
        conversation_id: str,
        material: StorySceneMaterial,
        draft: StorySceneOpeningDraft,
        now: datetime,
        language: str,
    ) -> StorySceneOpening:
        """Claim the scene, then write its messages — or undo the claim.

        Claim first so a racing replica loses before it writes anything
        player-visible; undo on an append failure so a scene that never
        reached the thread cannot leave the character locked out.
        """
        session = StorySceneSession.open_scene(
            character_id=character.id,
            conversation_id=conversation_id,
            source_layer=material.layer,
            arc_id=material.arc_id,
            beat_id=material.beat_id,
            title=draft.title or material.title,
            location=draft.location,
            mood=draft.mood,
            scene_type=material.scene_type,
            dramatic_question=material.dramatic_question,
            operator_position=material.operator_position,
            operator_note=material.operator_note,
            opened_at=now,
        )
        try:
            await self._sessions.add(session)
        except SceneSessionConflict as exc:
            raise SceneAlreadyOpen(character.id) from exc

        narration = Message(
            role=MessageRole.ASSISTANT,
            content=draft.narration,
            kind=MessageKind.SCENE_NARRATION,
            created_at=now,
        )
        character_message = Message(
            role=MessageRole.ASSISTANT,
            content=draft.character_line,
            created_at=now,
        )
        try:
            await self._append_opening_messages(
                conversation_id, [narration, character_message],
            )
        except Exception:
            await self._discard_session(session)
            raise

        await self._record_play_attempt(material, now=now)
        return StorySceneOpening(
            session=session,
            narration=narration,
            character_message=character_message,
            suggested_actions=await self._suggest_actions(
                character,
                session=session,
                recent_lines=(
                    draft.narration,
                    f"{character.name}：{draft.character_line}",
                ),
                language=language,
            ),
        )

    async def _suggest_actions(
        self,
        character: Character,
        *,
        session: StorySceneSession,
        recent_lines: tuple[str, ...],
        language: str,
    ) -> tuple[str, ...]:
        """Opening moves, or nothing at all.

        Runs *after* the scene is committed and swallows everything: the
        player has already been shown a scene (and, hosted, charged the
        one price for it), so a chips failure must never turn into a
        failed 起幕 — it turns into an empty chip row.
        """
        if self._chips_writer is None:
            return ()
        try:
            return await self._chips_writer.suggest_actions(
                StorySceneChipsContext(
                    character=character,
                    session=session,
                    recent_lines=recent_lines,
                    operator_primary_language=language,
                ),
            )
        except Exception:  # noqa: BLE001 - chips are never load-bearing
            _LOGGER.exception(
                "story scene chips failed at open scene=%s character=%s",
                session.id,
                character.id,
            )
            return ()

    async def _append_opening_messages(
        self, conversation_id: str, messages: list[Message],
    ) -> None:
        """The opening's two messages, or a failed action.

        The shared CAS helper answers with a bool; the opening is the
        caller for which a failed append is fatal (the claim is rolled
        back and, hosted, nothing is charged), so the translation to an
        exception happens here rather than in the helper."""
        if await append_scene_messages(
            self._conversations, conversation_id, messages,
        ):
            return
        raise SceneOpenFailed(
            f"could not append the scene opening to conversation "
            f"{conversation_id}",
        )

    async def _discard_session(self, session: StorySceneSession) -> None:
        """Best-effort rollback of the claim; never masks the real error."""
        try:
            await self._sessions.delete(session.id)
        except Exception:  # noqa: BLE001 - the caller is already failing
            _LOGGER.exception(
                "story scene rollback failed; session may be stuck open "
                "scene=%s character=%s",
                session.id,
                session.character_id,
            )

    async def _record_play_attempt(
        self, material: StorySceneMaterial, *, now: datetime,
    ) -> None:
        """Bookkeeping only — the scene is already open and playable.

        Fail-soft on purpose: losing the attempt record costs the prompt
        layer one fact, while raising here would fail an action the
        player has already been shown (and, hosted, charged for).
        """
        if material.beat_id is None or self._arcs is None:
            return
        try:
            await self._arcs.mark_beat_play_attempted(
                beat_id=material.beat_id,
                attempted_at=now,
                source=SCENE_OPEN_PLAY_SOURCE,
                result=SCENE_OPEN_PLAY_RESULT,
                push_intensity="player_pull",
            )
        except Exception:
            _LOGGER.exception(
                "story scene attempt record failed beat=%s",
                material.beat_id,
            )

    # ── collaborators ────────────────────────────────────────────────

    async def _resolve_conversation(
        self, character: Character,
    ) -> Conversation:
        """The character's web thread, created on demand.

        The scene plays inside the ordinary conversation (§5) — it is a
        presentation frame, not a separate transcript — so the player's
        history stays one continuous timeline.
        """
        conversation = await self._conversations.latest_for_character(
            character.id, source=SOURCE_WEB,
        )
        if conversation is not None:
            return conversation
        fresh = Conversation.start(
            character_id=character.id, source=SOURCE_WEB,
        )
        # Materialise the row before the CAS append: ``append_messages``
        # refuses to create conversations, by design.
        await self._conversations.save(fresh)
        return fresh

    async def _acquire_turn_lease(self, conversation_id: str):  # noqa: ANN201
        if self._turn_lease is None:
            return None
        return await self._turn_lease.acquire(conversation_id)

    async def _today_for_character(
        self, character: Character, now: datetime,
    ) -> date_type:
        return now.astimezone(
            await self._resolve_operator_timezone(character),
        ).date()

    async def _resolve_operator_timezone(self, character: Character) -> tzinfo:
        default = self._local_tz or timezone.utc
        service = self._operator_profile_service
        if service is None:
            return default
        try:
            operator = await service.get_for_user(
                getattr(character, "user_id", None) or "default",
            )
        except Exception:  # pragma: no cover - defensive
            return default
        if operator is None:
            return default
        return timezone_for_id(getattr(operator, "timezone_id", None))

    async def _resolve_operator_language(self, character: Character) -> str:
        default = "zh-TW"
        service = self._operator_profile_service
        if service is None:
            return default
        try:
            operator = await service.get_for_user(
                getattr(character, "user_id", None) or "default",
            )
        except Exception:  # pragma: no cover - defensive
            return default
        if operator is None:
            return default
        return (getattr(operator, "primary_language", "") or "").strip() or default


def _as_utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
