"""起幕 — the player pulls the next piece of story instead of waiting.

SC1-A implements the first layer of the §3.1 waterfall end to end: the
active arc's next pending beat becomes an opening (narration + the
character's first line + a visible scene frame), the opening lands in the
character's ordinary web thread, and a ``story_scene_sessions`` row marks
the character as being inside a scene until SC1-D/E close it.

Five behaviours are worth reading the code for, because they are choices
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
  by the same exception that fails the action (SC3-C). QG7 added a
  refusal that arrives *after* the writer has already run (the quality
  gate withholding a defective opening), so the covered upstream call is
  now held aside until the scene reaches the thread — see
  :func:`_deferred_opening_delivery` — and "charges nothing" stays true
  for it too.
* **The opening is reviewed before it is raised.** 起幕 is a background
  surface on the QG disposal table: an opening that fails a hard axis
  twice is not sent, and not sending it costs the player nothing they
  cannot recover by pressing the button again.
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
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date as date_type, datetime, timezone, tzinfo
from typing import Sequence

from kokoro_link.application.services.character_activity_anchor import (
    CharacterActivityAnchor,
)
from kokoro_link.application.services.chat_turn_lease import (
    ChatTurnLease,
    release_turn_lease,
)
from kokoro_link.application.services.cloud_action_billing_service import (
    CloudActionBillingService,
    NullActionBillingService,
)
from kokoro_link.application.services.output_quality import (
    OutputQualityOrchestrator,
    OutputQualityPolicy,
    script_mix_lines,
)
from kokoro_link.application.services.story_arc_service import StoryArcService
from kokoro_link.application.services.story_scene_closing import (
    SCENE_OPENING_QUALITY_SURFACE,
    SceneClosing,
    SceneClosingCoordinator,
    labelled_scene_segments,
)
from kokoro_link.application.services.story_scene_thread import (
    append_scene_messages,
)
from kokoro_link.contracts.cloud_action_billing import (
    ACTION_STORY_SCENE_OPEN,
    client_quoted_price,
)
from kokoro_link.contracts.embedder import EmbedderPort
from kokoro_link.contracts.interaction_context import (
    InteractionContext,
    confirm_deferred_deliveries,
    current_interaction,
    interaction_scope,
)
from kokoro_link.contracts.memory import MemoryRepositoryPort
from kokoro_link.contracts.novelty_gate import NoveltyGateContext
from kokoro_link.contracts.repositories import ConversationRepositoryPort
from kokoro_link.contracts.story_scene import (
    SCENE_NARRATION_SPEAKER,
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
    render_scene_line,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.operator_profile import DEFAULT_OPERATOR_ID
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
        # PP3 — the player's standing declaration about themselves, read
        # for both the opening and the wrap-up so one scene is staged from
        # one account of who the player is. Appended for the same
        # signature-compatibility reason as every block above.
        player_persona_note_repository=None,  # noqa: ANN001 - PlayerPersonaNoteRepositoryPort | None
        # NF4 — 起幕 is a paid foreground interaction, so opening a scene must
        # move the character's foreground-interaction anchor just like a chat
        # turn does. Without it a player who only presses 起幕 reads as
        # "never interacted" to dormancy / idle down-shift / freeze reaping.
        # Appended and optional for the same signature-compatibility reason as
        # every block above.
        activity_anchor: CharacterActivityAnchor | None = None,
        # QG0 — the shared review→regenerate→dispose band. Taken here and
        # handed straight down to the closing coordinator, because a
        # scene's two player-visible writes (the opening and the wrap-up)
        # are one surface as far as the disposal policy is concerned, and a
        # second injection point would let them drift apart. QG7 adopts it
        # on both ends; ``None`` leaves every path byte-identical.
        output_quality_orchestrator: OutputQualityOrchestrator | None = None,
        # QG7b — the container-wired knob QG7 could not reach (it did not
        # touch ``container.py``), so both ends of the scene were stuck
        # with QG7's hardcoded ``enabled=True, max_retries=1``. Threaded
        # to the closing coordinator below so the opening and the wrap-up
        # read one pair of values, same as they share one orchestrator.
        # Defaults preserve QG7's behaviour byte for byte.
        reply_quality_gate_enabled: bool = True,
        reply_quality_gate_max_retries: int = 1,
    ) -> None:
        self._sessions = sessions
        self._conversations = conversations
        self._opener = opener
        self._material_providers = tuple(material_providers)
        self._chips_writer = chips_writer
        self._arcs = story_arc_service
        self._turn_lease = turn_lease
        self._operator_profile_service = operator_profile_service
        self._quota_guard = quota_guard
        self._player_persona_note_repository = player_persona_note_repository
        self._activity_anchor = activity_anchor
        self._local_tz = local_tz
        self._action_billing: (
            CloudActionBillingService | NullActionBillingService
        ) = action_billing or NullActionBillingService()
        # Always built, even with no writer wired: closing a scene is a
        # state transition the player depends on ("結束場景" must work on
        # a self-host deployment with no model), and only the narration
        # depends on the writer.
        self._output_quality_orchestrator = output_quality_orchestrator
        self._reply_quality_gate_enabled = bool(reply_quality_gate_enabled)
        self._reply_quality_gate_max_retries = max(
            0, int(reply_quality_gate_max_retries),
        )
        self._closing = SceneClosingCoordinator(
            sessions=sessions,
            conversations=conversations,
            closer=closer,
            story_event_service=story_event_service,
            memory_repository=memory_repository,
            embedder=embedder,
            output_quality_orchestrator=output_quality_orchestrator,
            reply_quality_gate_enabled=reply_quality_gate_enabled,
            reply_quality_gate_max_retries=reply_quality_gate_max_retries,
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
        player_persona_note = await self._load_player_persona_note(character)
        lease = await self._acquire_turn_lease(conversation.id)
        try:
            # SC3-C — the one price, raised last (§3.4 red line 2).
            #
            # *Every* refusal above this line is a refusal the wallet never
            # hears about: an already-running scene, the per-tier daily
            # ceiling, an empty waterfall and a busy thread
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
                # QG7: everything the one price buys happens inside this
                # deferral, and the charge only counts the upstream call once
                # the scene is actually in the thread. See the helper.
                with _deferred_opening_delivery() as opening_delivered:
                    context = self._opening_context(
                        character,
                        material=material,
                        today=today,
                        language=language,
                        player_persona_note=player_persona_note,
                    )
                    draft = await self._write_opening(
                        character, context=context,
                    )
                    draft = await self._review_opening(
                        draft=draft, context=context,
                    )
                    if draft is None:
                        raise SceneOpenFailed(
                            "the story scene opening did not pass the "
                            "player-visible output quality gate",
                        )
                    opening = await self._persist_opening(
                        character,
                        conversation_id=conversation.id,
                        material=material,
                        draft=draft,
                        now=moment,
                        language=language,
                    )
                    opening_delivered()
                # NF4: the curtain is up and the player paid for it — that is
                # a foreground interaction with this character by every
                # definition the rest of the system uses. After the write, so
                # a refusal that never opened a scene is not counted as one;
                # fail-soft inside the anchor, so bookkeeping can never turn
                # a delivered opening into an error (and a refund for it).
                if self._activity_anchor is not None:
                    await self._activity_anchor.touch(character, now=moment)
                return opening
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
                player_persona_note=await self._load_player_persona_note(
                    character,
                ),
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
        # One read for both calls below: the verdict and the wrap-up it
        # may trigger must be staged from the same declaration.
        player_persona_note = await self._load_player_persona_note(character)
        verdict = await self._closing.judge(
            character,
            session=session,
            conversation=conversation,
            today=today,
            language=language,
            player_persona_note=player_persona_note,
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
            player_persona_note=player_persona_note,
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

    def _opening_context(
        self,
        character: Character,
        *,
        material: StorySceneMaterial,
        today: date_type,
        language: str,
        player_persona_note: str = "",
    ) -> StorySceneOpeningContext:
        """The staging for this opening, built once.

        Built once rather than per call because QG7 asks the opener for a
        second draft: an opening and its regeneration that disagreed about
        the date, the language or the player's declaration would be two
        different scenes, and the second one is the one the player would
        end up reading.
        """
        return StorySceneOpeningContext(
            character=character,
            material=material,
            today=today,
            operator_primary_language=language,
            player_persona_note=player_persona_note,
        )

    async def _write_opening(
        self,
        character: Character,
        *,
        context: StorySceneOpeningContext,
    ) -> StorySceneOpeningDraft:
        draft = await self._draft_opening(character, context=context)
        if draft is None:
            raise SceneOpenFailed(
                "the story scene opening could not be written",
            )
        return draft

    async def _draft_opening(
        self,
        character: Character,
        *,
        context: StorySceneOpeningContext,
    ) -> StorySceneOpeningDraft | None:
        """One call to the opener; ``None`` for every way it can fail.

        Split out of :meth:`_write_opening` so the quality gate's
        regeneration can ask for another draft without the exception the
        *first* draft's absence has to raise — the orchestrator reads a
        missing retry as "no second draft", which is a disposal, not an
        error.
        """
        try:
            return await self._opener.write_opening(context)
        except Exception:  # noqa: BLE001 - the port owns its own failures
            _LOGGER.exception(
                "story scene opener crashed character=%s layer=%s",
                character.id,
                context.material.layer,
            )
            return None

    async def _review_opening(
        self,
        *,
        draft: StorySceneOpeningDraft,
        context: StorySceneOpeningContext,
    ) -> StorySceneOpeningDraft | None:
        """Review the opening, regenerate it once, or refuse to raise it.

        ``None`` back means the curtain stays down. 起幕 is a background
        surface by the disposal table even though a player is waiting for
        it, and that is the right side of the table for exactly one reason:
        unlike a chat reply, an opening that is never sent costs the player
        nothing — the action fails, the session row is never created, the
        daily ceiling is untouched and (hosted) the charge is refunded in
        full, so they can simply press the button again. Shipping a leaked
        schema fragment as the first thing they read would cost the scene.
        """
        orchestrator = self._output_quality_orchestrator
        if orchestrator is None or not self._reply_quality_gate_enabled:
            return draft
        character = context.character
        layer = context.material.layer

        async def _regenerate(
            feedback: str,
        ) -> StorySceneOpeningDraft | None:
            # The feedback is logged (here and by the orchestrator) but not
            # delivered: ``StorySceneOpeningContext`` has no revision field
            # and the opener port is outside this ticket, so the retry is a
            # fresh sample rather than a directed rewrite.
            _LOGGER.info(
                "story scene opening regenerating after the quality gate "
                "character=%s layer=%s feedback=%s",
                character.id, layer, feedback,
            )
            retry = await self._draft_opening(character, context=context)
            if retry is None or not _has_opening_prose(retry):
                # Not a second draft. The orchestrator cannot see inside a
                # structured candidate to tell (``_is_blank`` only answers
                # for strings), so the emptiness rule is enforced here — an
                # opening with no narration or no first line is not an
                # opening the disposal table should re-review.
                return None
            return retry

        review = await orchestrator.review(
            draft,
            surface=SCENE_OPENING_QUALITY_SURFACE,
            context_for=lambda candidate: _opening_gate_context(
                candidate, context=context,
            ),
            regenerate=_regenerate,
            policy=OutputQualityPolicy.BACKGROUND_FAIL_CLOSED,
            character=character,
            max_retries=self._reply_quality_gate_max_retries,
            enabled=self._reply_quality_gate_enabled,
        )
        if review.skipped:
            _LOGGER.warning(
                "story scene opening withheld by the quality gate "
                "character=%s layer=%s — the curtain stays down and the "
                "action fails",
                character.id, layer,
            )
        return review.final

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
                    render_scene_line(
                        SCENE_NARRATION_SPEAKER, draft.narration,
                    ),
                    render_scene_line(character.name, draft.character_line),
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

    async def _load_player_persona_note(self, character: Character) -> str:
        """The player's declared identity for this pair, or nothing.

        Fail-soft in the same direction as the language resolve above: a
        scene that opens without the declaration is a weaker scene, a
        scene that fails to open is a charged button that did nothing.
        """
        repository = self._player_persona_note_repository
        if repository is None:
            return ""
        operator_id = getattr(character, "user_id", None) or DEFAULT_OPERATOR_ID
        try:
            row = await repository.get(
                character_id=character.id, operator_id=operator_id,
            )
        except Exception:
            _LOGGER.exception(
                "story scene: player persona note load failed character=%s",
                character.id,
            )
            return ""
        return row.note if row is not None else ""


# ── quality gate (QG7) ───────────────────────────────────────────────


def _has_opening_prose(draft: StorySceneOpeningDraft) -> bool:
    """Does this draft contain both halves of an opening?"""
    return bool(
        (draft.narration or "").strip()
        and (draft.character_line or "").strip()
    )


def _opening_gate_context(
    draft: StorySceneOpeningDraft,
    *,
    context: StorySceneOpeningContext,
) -> NoveltyGateContext:
    """What the judge reads about one opening.

    The two messages are reviewed as one body because that is how the
    player meets them — narration and the character's first line land in
    the same breath, and a line that reads as a non-sequitur only reads
    that way next to the narration it answers.

    ``known_material`` is the waterfall material verbatim. It is the one
    piece of context the opening is genuinely a *restatement* risk against:
    the material is an internal note the player has never read, so an
    opening that merely paraphrases it back is the failure the novelty axis
    exists for. The frame the player can already see (title, place, mood)
    is not here — the opener invents those, so they cannot be prior art.
    """
    character = context.character
    material = context.material
    return NoveltyGateContext(
        character_id=character.id,
        operator_id=getattr(character, "user_id", None) or DEFAULT_OPERATOR_ID,
        response_text=labelled_scene_segments(
            (SCENE_NARRATION_SPEAKER, draft.narration),
            (character.name, draft.character_line),
        ),
        known_material=_material_lines(material),
        # There is no user message — 起幕 is a button — so the closest true
        # statement of what was asked for goes here instead. Blank would be
        # worse: the register axes read this field to know what the reply is
        # answering, and 「（無）」 tells them the opening answers nothing.
        latest_user_message=(
            f"玩家按下「起幕」，要求現在就演一段《{material.title}》。"
        ),
        operator_primary_language=context.operator_primary_language,
        mechanical_evidence_lines=script_mix_lines(
            (draft.narration, draft.character_line),
            # Same language the judge is given as its reference for the
            # ``language_mismatch`` axis, so the deterministic evidence and
            # the rubric are measuring drift against the same script.
            primary_language=context.operator_primary_language,
        ),
    )


def _material_lines(material: StorySceneMaterial) -> tuple[str, ...]:
    """The chosen material as labelled lines. Blank fields are dropped."""
    fields: tuple[tuple[str, str | None], ...] = (
        ("本場標題", material.title),
        ("本場摘要", material.summary),
        ("劇情弧標題", material.arc_title),
        ("劇情弧前提", material.arc_premise),
        ("張力", material.tension),
        ("戲劇問題", material.dramatic_question),
        ("玩家在這場戲裡的位置", material.operator_position),
        ("玩家在場註記", material.operator_note),
    )
    return tuple(
        f"{label}：{(value or '').strip()}"
        for label, value in fields
        if (value or "").strip()
    )


@contextmanager
def _deferred_opening_delivery() -> Iterator[Callable[[], None]]:
    """Hold this action's covered-call record until the scene is delivered.

    The action charge is refunded or kept on one fact: did the Gateway
    serve a *covered* call under it. The LLM adapter answers that the
    moment prose comes back, which for 起幕 is one step too early — the
    thing the fixed price buys is **a scene in the player's thread**, and
    prose the quality gate refuses (QG7), or prose the thread refuses to
    take, never becomes one. Without this, a hard skip would fail the
    action *and* settle the charge: a player billed for an opening that
    never happened, which is the plan's §3.4 red line 2 verbatim.

    Media calls already solve exactly this by deferring their receipt until
    storage confirms it
    (:meth:`~kokoro_link.contracts.interaction_context.CoveredCallReceipt.defer_delivery`).
    The opening cannot, because the adapter that makes its call has no
    persistence step of its own to wait for — the persistence step is here.
    So the deferral is here too: the writer (and its regeneration, and the
    chips) run against a holding record, and only a delivered opening folds
    that record onto the charge. Never confirmed ⇒ never counted ⇒ the
    refund is whole, which is the same safe direction the receipt takes.

    Outside an action charge — self-host, a token-billed tier — there is
    nothing to hold and no scope is entered at all, so the opener sees the
    exact ``None`` interaction it saw before this existed.
    """
    outer = current_interaction()
    if outer is None:
        yield _no_delivery_to_confirm
        return
    holding = InteractionContext(
        interaction_id=outer.interaction_id, charge_id=outer.charge_id,
    )
    confirmed = False

    def _confirm() -> None:
        nonlocal confirmed
        if confirmed:
            return
        confirmed = True
        # Any receipt an adapter deferred is settled first, so a call that
        # only counts once its own persistence landed is counted here too.
        confirm_deferred_deliveries()
        for _ in range(holding.usage.covered_calls):
            outer.usage.mark_call_served()

    with interaction_scope(holding):
        yield _confirm


def _no_delivery_to_confirm() -> None:
    """Nothing was held, so there is nothing to fold back."""


def _as_utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
